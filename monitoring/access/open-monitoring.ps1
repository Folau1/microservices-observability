param(
    [ValidateSet('grafana', 'prometheus')]
    [string]$Service = 'grafana',
    [Parameter(Mandatory = $true)] [string]$IdentityFile,
    [Parameter(Mandatory = $true)] [string]$KnownHostsFile,
    [string]$ServerIp = '192.168.1.151',
    [string]$ServerUser = 'codex'
)

$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $IdentityFile -PathType Leaf)) { throw 'SSH identity file not found.' }
if (-not (Test-Path -LiteralPath $KnownHostsFile -PathType Leaf)) { throw 'SSH known-hosts file not found.' }
if ($Service -eq 'grafana') {
    $localPort = 13000
    $servicePort = 3000
} else {
    $localPort = 19090
    $servicePort = 9090
}
$kubeconfig = '/home/codex/.local/state/microservices-observability/monitoring/access/kubeconfig'
$remoteCommand = "KUBECONFIG=$kubeconfig /usr/local/bin/k3s kubectl -n observability port-forward svc/$Service ${localPort}:${servicePort} --address=127.0.0.1"
Write-Host "Оставьте это окно открытым и откройте http://127.0.0.1:$localPort/"
Write-Host 'Для завершения туннеля нажмите Ctrl+C.'
& ssh -T -i $IdentityFile -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes `
    -o "UserKnownHostsFile=$KnownHostsFile" -o ExitOnForwardFailure=yes `
    -L "127.0.0.1:${localPort}:127.0.0.1:${localPort}" "${ServerUser}@${ServerIp}" $remoteCommand
if ($LASTEXITCODE -ne 0) { throw "SSH tunnel exited with code $LASTEXITCODE. The limited Kubernetes token may need renewal." }
