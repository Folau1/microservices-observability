param(
    [Parameter(Mandatory = $true)] [string]$IdentityFile,
    [Parameter(Mandatory = $true)] [string]$KnownHostsFile,
    [string]$ServerIp = '192.168.1.151',
    [string]$ServerUser = 'codex',
    [string]$BindAddress = ''
)

$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $IdentityFile -PathType Leaf)) { throw 'SSH identity file not found.' }
if (-not (Test-Path -LiteralPath $KnownHostsFile -PathType Leaf)) { throw 'SSH known-hosts file not found.' }
$kubeconfig = '/home/codex/.local/state/microservices-observability/monitoring/access/kubeconfig'
$remoteCommand = "KUBECONFIG=$kubeconfig /usr/local/bin/k3s kubectl -n logging port-forward svc/kibana 15601:5601 --address=127.0.0.1"
Write-Host 'Оставьте окно открытым и откройте http://127.0.0.1:15601/'
Write-Host 'Для завершения туннеля нажмите Ctrl+C.'
$bindArgs = @()
if ($BindAddress) { $bindArgs = @('-b', $BindAddress) }
& ssh @bindArgs -T -i $IdentityFile -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes `
    -o "UserKnownHostsFile=$KnownHostsFile" -o ExitOnForwardFailure=yes `
    -L '127.0.0.1:15601:127.0.0.1:15601' "${ServerUser}@${ServerIp}" $remoteCommand
if ($LASTEXITCODE -ne 0) { throw "SSH tunnel exited with code $LASTEXITCODE. The limited Kubernetes token may need renewal." }
