param(
    [string]$HostIp = "YOUR_PI_IP",
    [string]$User = "pi",
    [string]$Dest = "~/assistant/pi"
)

Write-Host "Syncing brain files to $User@${HostIp}:${Dest}..." -ForegroundColor Cyan
scp ./pi/assistant_brains.py "${User}@${HostIp}:${Dest}/"
scp ./pi/config.py "${User}@${HostIp}:${Dest}/"
scp ./pi/HeyRobot.onnx "${User}@${HostIp}:${Dest}/"
scp ./pi/*.py "${User}@${HostIp}:${Dest}/"
scp ./pi/*.json "${User}@${HostIp}:${Dest}/"
# .env carries EMAIL_*/TTS_PRONUNCIATION_MAP secrets that config.py now reads
# from the environment — without it, email and pronunciation fixes silently break.
if (Test-Path ./pi/.env) {
    scp ./pi/.env "${User}@${HostIp}:${Dest}/.env"
}
if (Test-Path ./pi/firmware.bin) {
    scp ./pi/firmware.bin "${User}@${HostIp}:${Dest}/"
}
# Flask Jinja templates (globe_ui.html etc.) — without these, /globe returns
# HTTP 500 TemplateNotFound.
if (Test-Path ./pi/templates) {
    ssh -o StrictHostKeyChecking=no "${User}@${HostIp}" "mkdir -p ${Dest}/templates"
    scp ./pi/templates/* "${User}@${HostIp}:${Dest}/templates/"
}


# Private sidecar -- gitignored, deployed separately
if (Test-Path ./pi/adsb_proxy_pi.py) {
    scp ./pi/adsb_proxy_pi.py "${User}@${HostIp}:${Dest}/"
    Write-Host "ADS-B sidecar synced." -ForegroundColor Cyan
}
if (Test-Path ./pi/install_adsb_sidecar.sh) {
    scp ./pi/install_adsb_sidecar.sh "${User}@${HostIp}:${Dest}/"
    # sed strips CRLF in case the file was edited on Windows — bash chokes on \r
    ssh -o StrictHostKeyChecking=no "${User}@${HostIp}" "sed -i 's/\r$//' ${Dest}/install_adsb_sidecar.sh; chmod +x ${Dest}/install_adsb_sidecar.sh; bash ${Dest}/install_adsb_sidecar.sh"
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
