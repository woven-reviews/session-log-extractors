# Host transport only. All extraction, pending work, retries and upload use Python.
param([string]$Action, [string]$Harness = 'claude')
$ErrorActionPreference = 'Stop'
function Assert-SafePath([string]$Source) {
    if ($Source.Contains(',') -or $Source.Contains("`n") -or $Source.Contains("`r")) {
        throw 'Capture paths cannot contain commas or line breaks.'
    }
    $item = Get-Item -LiteralPath $Source -Force
    $resolved = $item.FullName.TrimEnd('\', '/')
    $ancestor = $item
    while ($null -ne $ancestor) {
        if ($ancestor.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'Capture refuses symbolic links and junctions in source paths.'
        }
        if ($ancestor -is [IO.FileInfo]) { $ancestor = $ancestor.Directory }
        else { $ancestor = $ancestor.Parent }
    }
    $homePath = [IO.Path]::GetFullPath([Environment]::GetFolderPath('UserProfile')).TrimEnd('\', '/')
    if ($homePath.Equals($resolved, [StringComparison]::OrdinalIgnoreCase) -or
        $homePath.StartsWith($resolved + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Refusing to mount the home directory or an ancestor.'
    }
    return $resolved
}
function Invoke-Capture {
    try { $root = (& git rev-parse --show-toplevel 2>$null) }
    catch { return }
    if ($LASTEXITCODE -ne 0 -or -not $root) { return }
    $marker = Join-Path $root '.session-log-capture'
    if (-not (Test-Path -LiteralPath $marker -PathType Leaf)) { return }
    if ($Harness -notin @('claude', 'codex', 'copilot')) { throw 'Unknown capture harness.' }
    $root = Assert-SafePath $root
    if ($Action -eq 'notice') {
        Write-Output '{"systemMessage":"Assessment session logs are captured to the session-logs branch. Explain setup failures in chat; the candidate repairs setup outside agent tools."}'
        return
    }
    if ($Action -notin @('pre', 'stop', 'doctor')) { throw 'Unknown capture action.' }
    $image = (Get-Content -LiteralPath $marker -Raw).Trim()
    if ($image -cnotmatch '^(ghcr\.io/woven-reviews/session-log-extractors@)?sha256:[a-f0-9]{64}$') {
        throw 'Configure the approved capture image by SHA-256 digest.'
    }
    $state = (& git -C $root rev-parse --path-format=absolute --git-path session-log-capture)
    if ($LASTEXITCODE -ne 0) { throw 'Cannot locate capture state.' }
    [IO.Directory]::CreateDirectory($state) | Out-Null
    $state = Assert-SafePath $state
    $utf8 = New-Object Text.UTF8Encoding $false
    $signal = $null
    if ($Action -eq 'stop') {
        [IO.Directory]::CreateDirectory((Join-Path $state 'incoming')) | Out-Null
        [IO.Directory]::CreateDirectory((Join-Path $state 'requests')) | Out-Null
        $signal = Join-Path $state ('incoming/' + [Guid]::NewGuid().ToString('N') + '.json')
        [IO.File]::WriteAllText($signal + '.tmp', [Console]::In.ReadToEnd(), $utf8)
        Move-Item -LiteralPath ($signal + '.tmp') -Destination $signal
    }
    try { $engine = (& docker info --format '{{.OSType}}' 2>$null) }
    catch { throw 'Install or start Docker outside the agent.' }
    if ($LASTEXITCODE -ne 0) { throw 'Install or start Docker outside the agent.' }
    if ($engine -ne 'linux') { throw 'Switch Docker to Linux containers outside the agent.' }
    try { & docker image inspect $image *> $null }
    catch { throw "Pull the configured image outside the agent: docker pull $image" }
    if ($LASTEXITCODE -ne 0) { throw "Pull the configured image outside the agent: docker pull $image" }
    $remote = (& git -C $root remote get-url origin)
    if ($LASTEXITCODE -ne 0) { throw 'Configure the assessment origin.' }
    $remote = $remote -replace '^git@github.com:', 'https://github.com/'
    if ($remote -cnotmatch '^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$') {
        throw 'Capture requires a GitHub HTTPS origin without embedded credentials.'
    }
    $env:SESSION_LOG_GIT_USERNAME = 'x-access-token'
    $env:SESSION_LOG_GIT_PASSWORD = $env:SESSION_LOG_TOKEN
    if (-not $env:SESSION_LOG_GIT_PASSWORD) {
        $env:GIT_TERMINAL_PROMPT = '0'
        $env:GCM_INTERACTIVE = 'Never'
        $credentials = ("url=$remote`n`n" | & git -C $root credential fill 2>$null)
        if ($LASTEXITCODE -ne 0) { throw 'Configure Git HTTPS authentication outside the agent.' }
        foreach ($line in $credentials) {
            if ($line.StartsWith('username=')) { $env:SESSION_LOG_GIT_USERNAME = $line.Substring(9) }
            if ($line.StartsWith('password=')) { $env:SESSION_LOG_GIT_PASSWORD = $line.Substring(9) }
        }
        $credentials = $null
    }
    if (-not $env:SESSION_LOG_GIT_USERNAME -or -not $env:SESSION_LOG_GIT_PASSWORD) {
        throw 'Git HTTPS credentials are unavailable.'
    }
    if ($Action -ne 'stop') { return }
    $copilotStore = $env:COPILOT_HOME
    if (-not $copilotStore) { $copilotStore = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.copilot' }
    $metadata = @(& docker run --rm --pull=never --network=none `
        --mount "type=bind,src=$signal,dst=/hook.json,readonly" --entrypoint python $image `
        -m session_log_extractors.capture_hook describe /hook.json `
        --harness $Harness --repo $root --remote $remote --copilot-home $copilotStore)
    if ($LASTEXITCODE -ne 0 -or $metadata.Count -ne 4) { throw 'Cannot decode capture input; signal retained.' }
    $transcript = $metadata[1]
    $stateLog = $metadata[2]
    Assert-SafePath $transcript | Out-Null
    if (-not (Test-Path -LiteralPath $transcript -PathType Leaf)) { throw 'Selected transcript must be a file.' }
    $destination = '/source/transcript'
    if ($Harness -eq 'copilot') { $destination = '/source/session-store.db' }
    $mounts = @('--mount', "type=bind,src=$root,dst=/source/repo,readonly", '--mount', "type=bind,src=$transcript,dst=$destination,readonly")
    if ($Harness -eq 'copilot') {
        Assert-SafePath $stateLog | Out-Null
        if (-not (Test-Path -LiteralPath $stateLog -PathType Leaf)) { throw 'Selected event log must be a file.' }
        $mounts += @('--mount', "type=bind,src=$stateLog,dst=/source/events.jsonl,readonly")
        foreach ($suffix in @('-wal', '-shm')) {
            if (Test-Path -LiteralPath ($transcript + $suffix)) {
                Assert-SafePath ($transcript + $suffix) | Out-Null
                $mounts += @('--mount', "type=bind,src=$transcript$suffix,dst=$destination$suffix,readonly")
            }
        }
    }
    $name = [IO.Path]::GetFileName($signal)
    $request = Join-Path $state ('requests/' + $name)
    [IO.File]::WriteAllText($request + '.tmp', $metadata[3], $utf8)
    Move-Item -LiteralPath ($request + '.tmp') -Destination $request
    & docker run --detach --rm --pull=never --label "org.woven.capture.state=$state" `
        --mount "type=bind,src=$state,dst=/state" @mounts `
        --env SESSION_LOG_GIT_USERNAME --env SESSION_LOG_GIT_PASSWORD `
        --entrypoint python $image -m session_log_extractors.capture_hook run "/state/requests/$name" `
        --state /state --harness $Harness --repo $root --remote $remote --copilot-home $copilotStore | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Cannot start capture; request retained.' }
}
try { Invoke-Capture; exit 0 }
catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    [Console]::Error.WriteLine('Explain the setup problem in chat. The candidate must fix it outside the agent, then retry.')
    if ($Action -eq 'stop') { exit 0 }
    exit 2
}
