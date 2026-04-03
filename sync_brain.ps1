param(
    [string]$HostIp = "192.168.4.34",
    [string]$User = "pi"
)

Write-Host "Syncing brain files to $User@${HostIp}..." -ForegroundColor Cyan
scp ./pi/assistant_brains.py "${User}@${HostIp}:~/assistant/"
scp ./pi/config.py "${User}@${HostIp}:~/assistant/"
scp ./pi/HeyRobot.onnx "${User}@${HostIp}:~/assistant/"

# Private sidecar -- gitignored, deployed separately
if (Test-Path ./pi/adsb_proxy_pi.py) {
    scp ./pi/adsb_proxy_pi.py "${User}@${HostIp}:~/assistant/"
    Write-Host "ADS-B sidecar synced." -ForegroundColor Cyan
}
if (Test-Path ./pi/install_adsb_sidecar.sh) {
    scp ./pi/install_adsb_sidecar.sh "${User}@${HostIp}:~/assistant/"
    ssh -o StrictHostKeyChecking=no "${User}@${HostIp}" "chmod +x ~/assistant/install_adsb_sidecar.sh; bash ~/assistant/install_adsb_sidecar.sh"
    Write-Host "ADS-B sidecar installed and started." -ForegroundColor Green
}

if ($LASTEXITCODE -ne 0) {
    Write-Host "Failed to copy files." -ForegroundColor Red
    exit 1
}

Write-Host "Restarting assistant.service on the Pi..." -ForegroundColor Cyan
ssh -o StrictHostKeyChecking=no "${User}@${HostIp}" "sudo systemctl restart assistant.service; sudo systemctl status assistant.service"

if ($LASTEXITCODE -eq 0) {
    Write-Host "Assistant successfully synced and restarted!" -ForegroundColor Green
} else {
    Write-Host "Service restart might have failed. Check the status above." -ForegroundColor Yellow
}
