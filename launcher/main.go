// The host launcher only checks readiness, persists requests and transports
// them to Docker. Interpretation, retries and Git uploads live in Python.
package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

const configName = ".session-log.json"
const (
	harnessClaude  = "claude"
	harnessCodex   = "codex"
	harnessCopilot = "copilot"
	actionPre      = "pre"
	actionStop     = "stop"
	actionNotice   = "notice"
	actionLaunch   = "launch"
	actionDoctor   = "doctor"
)

var sessionIdentifier = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]{0,199}$`)

const repair = "Session capture is not ready. Explain the reported setup problem in chat and ask the candidate to fix it outside the agent, then retry. Do not use assessment tools to repair setup."

type hookPayload struct {
	Cwd             string `json:"cwd"`
	Session         string `json:"session_id"`
	CamelSession    string `json:"sessionId"`
	Transcript      string `json:"transcript_path"`
	CamelTranscript string `json:"transcriptPath"`
}

type configuration struct {
	Version int    `json:"version"`
	Image   string `json:"image"`
}

type captureJob struct {
	Harness    string        `json:"harness"`
	Session    string        `json:"session_id"`
	Transcript string        `json:"transcript"`
	Repo       string        `json:"repo"`
	Remote     string        `json:"remote"`
	StateLog   string        `json:"state_log"`
	Mounts     []sourceMount `json:"mounts"`
}

func command(timeout time.Duration, input string, env []string, name string, args ...string) (string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, name, args...)
	cmd.Stdin = strings.NewReader(input)
	cmd.Env = append(os.Environ(), env...)
	output, err := cmd.Output()
	if err != nil {
		return "", fmt.Errorf("%s failed or timed out", name)
	}
	return strings.TrimSpace(string(output)), nil
}

func git(root string, args ...string) (string, error) {
	return command(2*time.Second, "", nil, "git", append([]string{"-C", root}, args...)...)
}

func assessment(cwd string) (string, configuration, error) {
	var config configuration
	root, err := git(cwd, "rev-parse", "--show-toplevel")
	if err != nil {
		for directory := filepath.Clean(cwd); ; directory = filepath.Dir(directory) {
			if _, statErr := os.Stat(filepath.Join(directory, configName)); statErr == nil {
				return directory, config, errors.New("Git is unavailable in a configured assessment checkout")
			}
			if filepath.Dir(directory) == directory {
				break
			}
		}
		return "", config, nil
	}
	data, err := os.ReadFile(filepath.Join(root, configName))
	if errors.Is(err, os.ErrNotExist) {
		return "", config, nil
	}
	if err != nil {
		return root, config, errors.New("cannot read capture configuration")
	}
	if json.Unmarshal(data, &config) != nil || config.Version != 1 {
		return root, config, errors.New("invalid capture configuration version")
	}
	digest := strings.TrimPrefix(config.Image, "ghcr.io/woven-reviews/session-log-extractors@")
	if !strings.HasPrefix(digest, "sha256:") {
		return root, config, errors.New("capture image must use the approved public ghcr.io digest")
	}
	digest = strings.TrimPrefix(digest, "sha256:")
	if _, err := hex.DecodeString(digest); err != nil || len(digest) != 64 {
		return root, config, errors.New("invalid capture image digest")
	}
	return root, config, nil
}

func normalizedRemote(raw string) (string, error) {
	if strings.HasPrefix(raw, "git@github.com:") {
		raw = "https://github.com/" + strings.TrimPrefix(raw, "git@github.com:")
	}
	u, err := url.Parse(raw)
	if err != nil || u.Scheme != "https" || u.Host != "github.com" || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
		return "", errors.New("capture requires a GitHub HTTPS remote without embedded credentials")
	}
	parts := strings.Split(strings.Trim(u.Path, "/"), "/")
	if len(parts) != 2 || parts[0] == "" || parts[1] == "" {
		return "", errors.New("invalid assessment repository remote")
	}
	return raw, nil
}

func credentials(root, remote string) ([]string, error) {
	if token := os.Getenv("SESSION_LOG_TOKEN"); token != "" {
		return []string{"SESSION_LOG_GIT_USERNAME=x-access-token", "SESSION_LOG_GIT_PASSWORD=" + token}, nil
	}
	input := "url=" + remote + "\n\n"
	output, err := command(2*time.Second, input, []string{"GIT_TERMINAL_PROMPT=0", "GCM_INTERACTIVE=Never"}, "git", "-C", root, "credential", "fill")
	if err != nil {
		return nil, errors.New("configure Git HTTPS authentication outside the agent, then retry")
	}
	var username, password string
	for _, line := range strings.Split(output, "\n") {
		if strings.HasPrefix(line, "username=") {
			username = strings.TrimPrefix(line, "username=")
		}
		if strings.HasPrefix(line, "password=") {
			password = strings.TrimPrefix(line, "password=")
		}
	}
	if username == "" || password == "" {
		return nil, errors.New("Git HTTPS credentials unavailable; authenticate outside the agent")
	}
	return []string{"SESSION_LOG_GIT_USERNAME=" + username, "SESSION_LOG_GIT_PASSWORD=" + password}, nil
}

func readiness(root string, config configuration) error {
	engine, err := command(2*time.Second, "", nil, "docker", "info", "--format", "{{.OSType}}")
	if err != nil {
		return errors.New("Docker is unavailable. Ask the candidate to install/start Docker Desktop (or Docker Engine on Linux) outside the agent")
	}
	if engine != "linux" {
		return errors.New("ask the candidate to switch Docker to Linux containers outside the agent")
	}
	if _, err := command(2*time.Second, "", nil, "docker", "image", "inspect", config.Image); err != nil {
		return fmt.Errorf("Ask the candidate to run docker pull %s outside the agent, then retry", config.Image)
	}
	raw, err := git(root, "remote", "get-url", "origin")
	if err != nil {
		return errors.New("assessment origin remote is unavailable")
	}
	remote, err := normalizedRemote(raw)
	if err != nil {
		return err
	}
	_, err = credentials(root, remote)
	return err
}

func stage(root, harness string, payload hookPayload) (string, error) {
	job, err := sourceRequest(harness, payload)
	if err != nil {
		return "", err
	}
	job.Repo = root
	raw, err := git(root, "remote", "get-url", "origin")
	if err != nil {
		return "", err
	}
	job.Remote, err = normalizedRemote(raw)
	if err != nil {
		return "", err
	}
	state, err := git(root, "rev-parse", "--path-format=absolute", "--git-path", "session-log-capture")
	if err != nil {
		return "", err
	}
	if err := persistRequest(state, job); err != nil {
		return "", err
	}
	return state, nil
}

func sourceRequest(harness string, payload hookPayload) (captureJob, error) {
	var job captureJob
	if harness != harnessClaude && harness != harnessCodex && harness != harnessCopilot {
		return job, errors.New("unknown capture harness")
	}
	if payload.Session == "" {
		payload.Session = payload.CamelSession
	}
	if payload.Transcript == "" {
		payload.Transcript = payload.CamelTranscript
	}
	if !sessionIdentifier.MatchString(payload.Session) {
		return job, errors.New("hook did not record a valid session ID")
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return job, err
	}
	if harness == harnessCopilot {
		copilotHome := os.Getenv("COPILOT_HOME")
		if copilotHome == "" {
			copilotHome = filepath.Join(home, ".copilot")
		}
		payload.Transcript = filepath.Join(copilotHome, "session-store.db")
	}
	if payload.Transcript == "" {
		return job, errors.New("hook did not record a transcript path")
	}
	payload.Transcript, err = filepath.Abs(payload.Transcript)
	if err != nil {
		return job, err
	}
	job = captureJob{Harness: harness, Session: payload.Session, Transcript: payload.Transcript}
	if harness == harnessCopilot {
		copilotHome := filepath.Dir(payload.Transcript)
		job.StateLog = filepath.Join(copilotHome, "session-state", payload.Session, "events.jsonl")
	}
	job.Mounts, err = sessionMounts(job, home)
	return job, err
}

func persistRequest(state string, job captureJob) error {
	pending := filepath.Join(state, "requests")
	if err := os.MkdirAll(pending, 0700); err != nil {
		return err
	}
	data, err := json.Marshal(job)
	if err != nil {
		return err
	}
	id := make([]byte, 16)
	if _, err := rand.Read(id); err != nil {
		return err
	}
	tmp := filepath.Join(pending, hex.EncodeToString(id)+".tmp")
	if err := os.WriteFile(tmp, data, 0600); err != nil {
		return err
	}
	if err := os.Rename(tmp, strings.TrimSuffix(tmp, ".tmp")+".json"); err != nil {
		return err
	}
	return nil
}

func handoff(root, state string) error {
	executable, err := os.Executable()
	if err != nil {
		return err
	}
	log, err := os.OpenFile(filepath.Join(state, "launcher.log"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600)
	if err != nil {
		return err
	}
	defer log.Close()
	cmd := exec.Command(executable, actionLaunch, root, state)
	cmd.Stdout, cmd.Stderr = log, log
	detach(cmd)
	if err := cmd.Start(); err != nil {
		return errors.New("cannot hand capture to a detached launcher")
	}
	return cmd.Process.Release()
}

func launch(root, state string) error {
	home, err := os.UserHomeDir()
	if err != nil {
		return err
	}
	if err := validateMount(state, home); err != nil {
		return err
	}
	_, config, err := assessment(root)
	if err != nil {
		return err
	}
	if config.Image == "" {
		return errors.New("capture configuration was removed; pending work retained")
	}
	raw, err := git(root, "remote", "get-url", "origin")
	if err != nil {
		return err
	}
	remote, err := normalizedRemote(raw)
	if err != nil {
		return err
	}
	env, err := credentials(root, remote)
	if err != nil {
		return err
	}
	return launchRequests(launchContext{root, state, home, config.Image, remote, env})
}

func validateMount(source, home string) error {
	resolvedSource, err := filepath.EvalSymlinks(source)
	if err != nil {
		return err
	}
	resolvedHome, err := filepath.EvalSymlinks(home)
	if err != nil {
		return err
	}
	relative, err := filepath.Rel(resolvedSource, resolvedHome)
	if err == nil && relative != ".." && !strings.HasPrefix(relative, ".."+string(filepath.Separator)) {
		return errors.New("refusing to mount the home directory or a parent directory")
	}
	return nil
}

func run(args []string, input io.Reader, output io.Writer) int {
	if len(args) == 1 && args[0] == actionDoctor {
		return doctor(output)
	}
	if len(args) == 3 && args[0] == actionLaunch {
		if err := launch(args[1], args[2]); err != nil {
			fmt.Fprintln(output, err)
			return 1
		}
		return 0
	}
	if len(args) != 2 {
		fmt.Fprintln(output, "usage: session-log-hook pre|stop|notice claude|codex|copilot")
		return 2
	}
	var payload hookPayload
	if json.NewDecoder(io.LimitReader(input, 4*1024*1024)).Decode(&payload) != nil {
		fmt.Fprintln(output, "invalid hook input")
		return 2
	}
	if payload.Cwd == "" {
		payload.Cwd, _ = os.Getwd()
	}
	root, config, err := assessment(payload.Cwd)
	if root == "" {
		return 0
	}
	if err != nil {
		fmt.Fprintln(output, err)
		if args[0] == actionPre {
			return 2
		}
		return 0
	}
	if args[0] == actionPre {
		if _, err := sourceRequest(args[1], payload); err != nil {
			fmt.Fprintln(output, err)
			return 2
		}
		if err := readiness(root, config); err != nil {
			fmt.Fprintln(output, err, repair)
			return 2
		}
		return 0
	}
	if args[0] == actionNotice {
		fmt.Fprintln(output, `{"systemMessage":"Assessment session logs are captured to the session-logs branch. If Docker is unavailable, explain setup and let the candidate repair it outside agent tools."}`)
		return 0
	}
	if args[0] != actionStop {
		fmt.Fprintln(output, "unknown capture action")
		return 2
	}
	state, err := stage(root, args[1], payload)
	if err == nil {
		err = handoff(root, state)
	}
	if err != nil {
		fmt.Fprintln(output, err)
		return 0 // Stop must leave conversational setup and repair instructions usable.
	}
	return 0
}

func main() {
	output := os.Stderr
	if len(os.Args) > 1 && os.Args[1] == actionNotice {
		output = os.Stdout
	}
	os.Exit(run(os.Args[1:], os.Stdin, output))
}

func doctor(output io.Writer) int {
	cwd, err := os.Getwd()
	if err != nil {
		fmt.Fprintln(output, err)
		return 1
	}
	root, config, err := assessment(cwd)
	if root == "" {
		fmt.Fprintln(output, "Session capture is inactive in this checkout")
		return 0
	}
	if err == nil {
		err = readiness(root, config)
	}
	if err != nil {
		fmt.Fprintln(output, err)
		return 1
	}
	fmt.Fprintln(output, "Docker, capture image and Git HTTPS credentials are ready")
	return 0
}
