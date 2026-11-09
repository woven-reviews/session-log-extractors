package main

import (
	"bytes"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

func testRepository(t *testing.T) string {
	t.Helper()
	root := t.TempDir()
	cmd := exec.Command("git", "init", "-q", root)
	if output, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("git init: %s: %v", output, err)
	}
	cmd = exec.Command("git", "-C", root, "remote", "add", "origin", "git@github.com:woven-reviews/probe.git")
	if err := cmd.Run(); err != nil {
		t.Fatal(err)
	}
	return root
}

func testConfig(t *testing.T, root string) configuration {
	t.Helper()
	config := configuration{1, "ghcr.io/woven-reviews/session-log-extractors@sha256:" + strings.Repeat("a", 64)}
	data, _ := json.Marshal(config)
	if err := os.WriteFile(filepath.Join(root, configName), data, 0600); err != nil {
		t.Fatal(err)
	}
	return config
}

func TestRemoteNormalization(t *testing.T) {
	for _, remote := range []string{"git@github.com:woven-reviews/probe.git", "https://github.com/woven-reviews/probe.git"} {
		actual, err := normalizedRemote(remote)
		if err != nil || actual != "https://github.com/woven-reviews/probe.git" {
			t.Fatalf("%q: %q %v", remote, actual, err)
		}
	}
	for _, remote := range []string{"file:///tmp/repo", "https://secret@github.com/o/r", "https://github.com/o/r?token=secret", "https://github.com/o/r#ref", "https://other.invalid/o/r", "https://github.com/one"} {
		if _, err := normalizedRemote(remote); err == nil {
			t.Errorf("accepted %q", remote)
		}
	}
}

func TestInactiveCheckoutDoesNothing(t *testing.T) {
	root := testRepository(t)
	var output bytes.Buffer
	code := run([]string{"pre", "claude"}, strings.NewReader(`{"cwd":`+quote(root)+`}`), &output)
	if code != 0 || output.Len() != 0 {
		t.Fatalf("inactive hook: %d %s", code, output.String())
	}
	if _, err := os.Stat(filepath.Join(root, ".git", "session-log-capture")); !os.IsNotExist(err) {
		t.Fatal("inactive hook wrote capture state")
	}
}

func TestConfiguredCheckoutDoesNotBecomeInactiveWhenGitFails(t *testing.T) {
	root := testRepository(t)
	testConfig(t, root)
	t.Setenv("PATH", t.TempDir())
	var output bytes.Buffer
	code := run([]string{actionPre, harnessClaude}, strings.NewReader(`{"cwd":`+quote(root)+`}`), &output)
	if code != 2 {
		t.Fatalf("configured checkout bypassed readiness: %d", code)
	}
}

func TestInvalidConfigurationLeavesConversationAvailable(t *testing.T) {
	root := testRepository(t)
	if err := os.WriteFile(filepath.Join(root, configName), []byte("{}"), 0600); err != nil {
		t.Fatal(err)
	}
	var output bytes.Buffer
	code := run([]string{actionStop, harnessClaude}, strings.NewReader(`{"cwd":`+quote(root)+`}`), &output)
	if code != 0 {
		t.Fatalf("setup conversation was blocked: %d", code)
	}
}

func quote(value string) string { data, _ := json.Marshal(value); return string(data) }

func TestConfiguredAssessmentAndInvalidImage(t *testing.T) {
	root := testRepository(t)
	testConfig(t, root)
	actual, _, err := assessment(root)
	if err != nil || actual == "" {
		t.Fatalf("assessment: %q %v", actual, err)
	}
	for _, config := range []string{`{}`, `{"version":1,"image":"python:latest"}`, `{"version":1,"image":"ghcr.io/woven-reviews/session-log-extractors@sha256:bad"}`} {
		if err := os.WriteFile(filepath.Join(root, configName), []byte(config), 0600); err != nil {
			t.Fatal(err)
		}
		_, _, err := assessment(root)
		if err == nil {
			t.Fatalf("accepted config %s", config)
		}
	}
}

func TestLocalImmutableImageForMaintainerVerification(t *testing.T) {
	root := testRepository(t)
	config := configuration{1, "sha256:" + strings.Repeat("b", 64)}
	data, _ := json.Marshal(config)
	if err := os.WriteFile(filepath.Join(root, configName), data, 0600); err != nil {
		t.Fatal(err)
	}
	if _, _, err := assessment(root); err != nil {
		t.Fatal(err)
	}
}

func TestHomeDirectoryMountIsForbidden(t *testing.T) {
	root := t.TempDir()
	home := filepath.Join(root, "home")
	project := filepath.Join(home, "project")
	if err := os.MkdirAll(project, 0700); err != nil {
		t.Fatal(err)
	}
	for _, source := range []string{home, root} {
		if err := validateMount(source, home); err == nil {
			t.Fatalf("accepted forbidden mount %q", source)
		}
	}
	if err := validateMount(project, home); err != nil {
		t.Fatal(err)
	}
	alias := filepath.Join(root, "home-alias")
	if err := os.Symlink(home, alias); err == nil {
		if err := validateMount(alias, home); err == nil {
			t.Fatal("accepted symlink to home")
		}
	}
}

func TestStagePersistsBothHookFormats(t *testing.T) {
	root := testRepository(t)
	if err := os.WriteFile(filepath.Join(root, "native.jsonl"), []byte("{}\n"), 0600); err != nil {
		t.Fatal(err)
	}
	for _, payload := range []hookPayload{
		{Session: "recorded-session", Transcript: filepath.Join(root, "native.jsonl")},
		{CamelSession: "recorded-session", CamelTranscript: filepath.Join(root, "native.jsonl")},
	} {
		state, err := stage(root, "claude", payload)
		if err != nil {
			t.Fatal(err)
		}
		paths, err := filepath.Glob(filepath.Join(state, "requests", "*.json"))
		if err != nil || len(paths) == 0 {
			t.Fatalf("no pending job: %v", err)
		}
		data, err := os.ReadFile(paths[0])
		if err != nil {
			t.Fatal(err)
		}
		var job captureJob
		if err := json.Unmarshal(data, &job); err != nil {
			t.Fatal(err)
		}
		if job.Session != "recorded-session" || job.Remote != "https://github.com/woven-reviews/probe.git" || len(job.Mounts) != 1 || job.Mounts[0].Recorded != filepath.Join(root, "native.jsonl") {
			t.Fatalf("invalid request: %+v", job)
		}
	}
}

func TestSourceMountsAdmitFilesOnly(t *testing.T) {
	root := t.TempDir()
	home := filepath.Join(root, "home")
	session := filepath.Join(home, "session")
	if err := os.MkdirAll(session, 0700); err != nil {
		t.Fatal(err)
	}
	transcript := filepath.Join(session, "native.jsonl")
	if err := os.WriteFile(transcript, []byte("{}\n"), 0600); err != nil {
		t.Fatal(err)
	}
	mount, err := mountFile(transcript, home)
	if err != nil || mount.Recorded != transcript {
		t.Fatalf("file mount: %+v %v", mount, err)
	}
	for _, source := range []string{home, root, session} {
		if _, err := mountFile(source, home); err == nil {
			t.Fatalf("admitted directory %s", source)
		}
	}
	args := sourceArguments([]sourceMount{mount})
	if len(args) != 2 || !strings.HasSuffix(args[1], ",readonly") {
		t.Fatalf("source is not read-only: %v", args)
	}
}

func TestPendingRequestCannotRedirectUploadCredentials(t *testing.T) {
	root := testRepository(t)
	request := filepath.Join(root, "request.json")
	data, _ := json.Marshal(captureJob{Repo: root, Remote: "https://other.invalid/stolen"})
	if err := os.WriteFile(request, data, 0600); err != nil {
		t.Fatal(err)
	}
	context := launchContext{Root: root, Remote: "https://github.com/woven-reviews/probe.git"}
	if _, err := prepareRequest(context, request); err == nil || !strings.Contains(err.Error(), "assessment") {
		t.Fatalf("redirect accepted: %v", err)
	}
}

func TestMissingEvidenceCannotStage(t *testing.T) {
	root := testRepository(t)
	for _, payload := range []hookPayload{{}, {Session: "id"}} {
		if _, err := stage(root, "claude", payload); err == nil {
			t.Fatal("staged without required evidence")
		}
	}
	if _, err := stage(root, "unknown", hookPayload{}); err == nil {
		t.Fatal("staged unknown harness")
	}
}

func TestReadinessExplainsUnavailableDocker(t *testing.T) {
	t.Setenv("PATH", t.TempDir())
	err := readiness(t.TempDir(), configuration{})
	if err == nil || !strings.Contains(err.Error(), "outside the agent") {
		t.Fatalf("readiness: %v", err)
	}
}

func TestExplicitRuntimeTokenStaysOutOfRequests(t *testing.T) {
	t.Setenv("SESSION_LOG_TOKEN", "synthetic-secret")
	env, err := credentials("unused", "https://github.com/o/r")
	if err != nil || len(env) != 2 || env[1] != "SESSION_LOG_GIT_PASSWORD=synthetic-secret" {
		t.Fatalf("credential transfer failed: %v", err)
	}
}

func TestCommandTimeoutHelper(t *testing.T) {
	if os.Getenv("QUAL_2323_TIMEOUT_HELPER") == "1" {
		time.Sleep(time.Minute)
		return
	}
	_, err := command(20*time.Millisecond, "", []string{"QUAL_2323_TIMEOUT_HELPER=1"}, os.Args[0], "-test.run=^TestCommandTimeoutHelper$")
	if err == nil {
		t.Fatal("unbounded command")
	}
}

func TestHookUsageAndNotice(t *testing.T) {
	var output bytes.Buffer
	if code := run(nil, strings.NewReader("{}"), &output); code != 2 {
		t.Fatal(code)
	}
	if code := run([]string{"pre", "claude"}, strings.NewReader("not JSON"), &output); code != 2 {
		t.Fatal(code)
	}
	root := testRepository(t)
	testConfig(t, root)
	output.Reset()
	if code := run([]string{"notice", "claude"}, strings.NewReader(`{"cwd":`+quote(root)+`}`), &output); code != 0 {
		t.Fatal(code)
	}
	if !strings.Contains(output.String(), "session-logs") {
		t.Fatal("missing capture notice")
	}
}

func TestCodexPluginCommandsRunFromPathsWithSpaces(t *testing.T) {
	root := testRepository(t)
	plugin := filepath.Join(t.TempDir(), "plugin with spaces")
	binary := filepath.Join(plugin, "bin", "session-log-hook.exe")
	if err := os.MkdirAll(filepath.Dir(binary), 0700); err != nil {
		t.Fatal(err)
	}
	if output, err := exec.Command("go", "build", "-o", binary, ".").CombinedOutput(); err != nil {
		t.Fatalf("build: %s %v", output, err)
	}
	data, err := os.ReadFile(filepath.Join("..", "plugins", "codex", "hooks", "hooks.json"))
	if err != nil {
		t.Fatal(err)
	}
	type commandEntry struct {
		Command string `json:"command"`
		Windows string `json:"commandWindows"`
	}
	type group struct {
		Entries []commandEntry `json:"hooks"`
	}
	var definitions struct {
		Events map[string][]group `json:"hooks"`
	}
	if err := json.Unmarshal(data, &definitions); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PLUGIN_ROOT", plugin)
	for _, event := range []string{"PreToolUse", "Stop", "SessionStart"} {
		definition := definitions.Events[event][0].Entries[0]
		var cmd *exec.Cmd
		if runtime.GOOS == "windows" {
			cmd = exec.Command("cmd.exe", "/d", "/s", "/c", definition.Windows)
		} else {
			cmd = exec.Command("sh", "-c", definition.Command)
		}
		cmd.Stdin = strings.NewReader(`{"cwd":` + quote(root) + `}`)
		if output, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("%s: %s %v", event, output, err)
		}
	}
}

func TestCopilotAdmitsOnlySelectedSessionFiles(t *testing.T) {
	store := t.TempDir()
	t.Setenv("COPILOT_HOME", store)
	events := filepath.Join(store, "session-state", "selected-session", "events.jsonl")
	if err := os.MkdirAll(filepath.Dir(events), 0700); err != nil {
		t.Fatal(err)
	}
	files := []string{filepath.Join(store, "session-store.db"), filepath.Join(store, "session-store.db-wal"), filepath.Join(store, "session-store.db-shm"), events}
	for _, file := range files {
		if err := os.WriteFile(file, []byte("fixture"), 0600); err != nil {
			t.Fatal(err)
		}
	}
	job, err := sourceRequest(harnessCopilot, hookPayload{CamelSession: "selected-session"})
	if err != nil {
		t.Fatal(err)
	}
	if job.StateLog != events || len(job.Mounts) != len(files) {
		t.Fatalf("wrong session sources: %+v", job)
	}
	for _, mount := range job.Mounts {
		info, err := os.Stat(mount.Recorded)
		if err != nil || !info.Mode().IsRegular() {
			t.Fatalf("non-file source admitted: %+v", mount)
		}
	}
}

func TestDetachedChildSurvivesParentExit(t *testing.T) {
	directory := t.TempDir()
	parent := exec.Command(os.Args[0], "-test.run=^TestDetachedHelper$")
	parent.Env = append(os.Environ(), "QUAL_2323_DETACH_PHASE=parent", "QUAL_2323_DETACH_DIRECTORY="+directory)
	if output, err := parent.CombinedOutput(); err != nil {
		t.Fatalf("parent: %s %v", output, err)
	}
	if err := os.WriteFile(filepath.Join(directory, "release"), []byte("ready"), 0600); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if _, err := os.Stat(filepath.Join(directory, "completed")); err == nil {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("detached child did not finish after its parent exited")
}

func TestDetachedHelper(t *testing.T) {
	phase := os.Getenv("QUAL_2323_DETACH_PHASE")
	if phase == "" {
		return
	}
	directory := os.Getenv("QUAL_2323_DETACH_DIRECTORY")
	if phase == "parent" {
		log, err := os.Create(filepath.Join(directory, "child.log"))
		if err != nil {
			t.Fatal(err)
		}
		child := exec.Command(os.Args[0], "-test.run=^TestDetachedHelper$")
		child.Env = append(os.Environ(), "QUAL_2323_DETACH_PHASE=child")
		child.Stdout, child.Stderr = log, log
		detach(child)
		if err := child.Start(); err != nil {
			t.Fatal(err)
		}
		if err := child.Process.Release(); err != nil {
			t.Fatal(err)
		}
		os.Exit(0)
	}
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if _, err := os.Stat(filepath.Join(directory, "release")); err == nil {
			if err := os.WriteFile(filepath.Join(directory, "completed"), []byte("done"), 0600); err != nil {
				t.Fatal(err)
			}
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("parent never released the detached child")
}
