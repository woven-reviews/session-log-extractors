package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path"
	"path/filepath"
	"strings"
	"time"
)

type sourceMount struct {
	Recorded string `json:"recorded"`
	Mounted  string `json:"mounted"`
}

type launchContext struct {
	Root        string
	State       string
	Home        string
	Image       string
	Remote      string
	Credentials []string
}

func mountFile(source, home string) (sourceMount, error) {
	if err := validateMount(source, home); err != nil {
		return sourceMount{}, err
	}
	info, err := os.Stat(source)
	if err != nil {
		return sourceMount{}, err
	}
	if !info.Mode().IsRegular() {
		return sourceMount{}, errors.New("capture source must be a regular file")
	}
	if strings.ContainsAny(source, ",\r\n") {
		return sourceMount{}, errors.New("capture source path cannot contain mount delimiters")
	}
	// Group sibling SQLite DB/WAL/SHM files at the same container directory.
	digest := sha256.Sum256([]byte(filepath.Dir(source)))
	destination := path.Join("/sources", hex.EncodeToString(digest[:]), filepath.Base(source))
	return sourceMount{source, destination}, nil
}

func sessionMounts(job captureJob, home string) ([]sourceMount, error) {
	files := []string{job.Transcript}
	if job.StateLog != "" {
		files = append(files, job.StateLog)
	}
	if job.Harness == harnessCopilot {
		for _, suffix := range []string{"-wal", "-shm"} {
			if _, err := os.Stat(job.Transcript + suffix); err == nil {
				files = append(files, job.Transcript+suffix)
			}
		}
	}
	mounts := make([]sourceMount, 0, len(files))
	for _, file := range files {
		mount, err := mountFile(file, home)
		if err != nil {
			return nil, err
		}
		mounts = append(mounts, mount)
	}
	return mounts, nil
}

func sourceArguments(mounts []sourceMount) []string {
	var args []string
	for _, mount := range mounts {
		args = append(args, "--mount", "type=bind,src="+mount.Recorded+",dst="+mount.Mounted+",readonly")
	}
	return args
}

func prepareRequest(context launchContext, request string) (captureJob, error) {
	var job captureJob
	data, err := os.ReadFile(request)
	if err != nil {
		return job, err
	}
	if err = json.Unmarshal(data, &job); err != nil {
		return job, err
	}
	if job.Remote != context.Remote || filepath.Clean(job.Repo) != filepath.Clean(context.Root) {
		return job, errors.New("pending request does not match the configured assessment")
	}
	// Refresh source admission when recovering persisted work; never trust old
	// mount entries left by a prior launcher version.
	job.Mounts, err = sessionMounts(job, context.Home)
	if err != nil {
		return job, err
	}
	data, err = json.Marshal(job)
	if err != nil {
		return job, err
	}
	inspectionFile, err := os.CreateTemp(filepath.Dir(request), "inspection-*.tmp")
	if err != nil {
		return job, err
	}
	inspection := inspectionFile.Name()
	defer os.Remove(inspection)
	if _, err = inspectionFile.Write(data); err != nil {
		inspectionFile.Close()
		return job, err
	}
	if err = inspectionFile.Close(); err != nil {
		return job, err
	}
	args := []string{"run", "--rm", "--network=none", "--label", "org.woven.capture.state=" + context.State, "--mount", "type=bind,src=" + inspection + ",dst=/request.json,readonly"}
	args = append(args, sourceArguments(job.Mounts)...)
	args = append(args, "--entrypoint", "python3", context.Image, "-m", "session_log_extractors.capture_extraction", "/request.json")
	output, err := command(30*time.Minute, "", nil, "docker", args...)
	if err != nil {
		return job, err
	}
	var assets []string
	if err = json.Unmarshal([]byte(output), &assets); err != nil {
		return job, errors.New("capture reader returned invalid image source paths")
	}
	for _, asset := range assets {
		mount, err := mountFile(asset, context.Home)
		if err != nil {
			continue
		} // Missing assets remain explicitly unavailable.
		job.Mounts = append(job.Mounts, mount)
	}
	return job, nil
}

func launchRequest(context launchContext, request string) error {
	name := filepath.Base(request)
	digest := sha256.Sum256([]byte(context.State + name))
	workerName := "session-log-" + hex.EncodeToString(digest[:16])
	running, _ := command(2*time.Second, "", nil, "docker", "inspect", "--format", "{{.State.Running}}", workerName)
	if running == "true" {
		return nil
	}
	job, err := prepareRequest(context, request)
	if err != nil {
		return err
	}
	data, err := json.Marshal(job)
	if err != nil {
		return err
	}
	pending := filepath.Join(context.State, "pending")
	if err = os.MkdirAll(pending, 0700); err != nil {
		return err
	}
	destination := filepath.Join(pending, name)
	if err = os.WriteFile(destination+".tmp", data, 0600); err != nil {
		return err
	}
	if err = os.Rename(destination+".tmp", destination); err != nil {
		return err
	}
	if filepath.Dir(request) != pending {
		_ = os.Remove(request)
	}
	args := []string{"run", "--detach", "--rm", "--name", workerName, "--label", "org.woven.capture.state=" + context.State, "--env", "SESSION_LOG_GIT_USERNAME", "--env", "SESSION_LOG_GIT_PASSWORD",
		"--mount", "type=bind,src=" + context.State + ",dst=/state"}
	args = append(args, sourceArguments(job.Mounts)...)
	args = append(args, "--entrypoint", "python3", context.Image, "-m", "session_log_extractors.capture_runtime", "/state", "--request", name)
	_, err = command(30*time.Minute, "", context.Credentials, "docker", args...)
	return err
}

func launchRequests(context launchContext) error {
	var lastError error
	for _, directory := range []string{"pending", "requests"} {
		requests, err := filepath.Glob(filepath.Join(context.State, directory, "*.json"))
		if err != nil {
			return err
		}
		for _, request := range requests {
			if err := launchRequest(context, request); err != nil {
				lastError = err
				fmt.Fprintln(os.Stderr, err)
			}
		}
	}
	return lastError
}
