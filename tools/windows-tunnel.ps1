# Keep this process running while using the Jetson-backed UI locally.
param([string]$SshHost = 'jetson', [int]$LocalPort = 8021)
$ErrorActionPreference = 'Stop'
& ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 `
    -L "127.0.0.1:${LocalPort}:127.0.0.1:9020" $SshHost
