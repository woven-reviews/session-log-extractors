package main

import (
	"os/exec"
	"syscall"
)

func detach(cmd *exec.Cmd) {
	// Separate console/process group; no inherited standard handles.
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: 0x00000008 | 0x00000200}
}
