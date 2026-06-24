param(
    [string]$HostIp = "192.168.4.205",
    [string]$User = "penneyd",
    [string]$Dest = "/tmp/esp32_flash",
    [string]$SshKey = "C:\apps\esp32_round\tachyon_key_fixed",
    [string]$Port = "/dev/ttyHS2"
)

$BuildDir = ".\.pio\build\esp32-s3-devkitc-1"

if (-not (Test-Path "$BuildDir\firmware.bin")) {
    Write-Host "Firmware not found. Building first..." -ForegroundColor Yellow
    pio run
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Build failed. Exiting." -ForegroundColor Red
        exit 1
    }
}

Write-Host "Creating upload directory on Tachyon..." -ForegroundColor Cyan
ssh -i "$SshKey" -o StrictHostKeyChecking=no "${User}@${HostIp}" "mkdir -p ${Dest}"

Write-Host "Copying ESP32 binaries to Tachyon..." -ForegroundColor Cyan
scp -i "$SshKey" -o StrictHostKeyChecking=no "$BuildDir\bootloader.bin" "${User}@${HostIp}:${Dest}/"
scp -i "$SshKey" -o StrictHostKeyChecking=no "$BuildDir\partitions.bin" "${User}@${HostIp}:${Dest}/"
scp -i "$SshKey" -o StrictHostKeyChecking=no "$BuildDir\firmware.bin" "${User}@${HostIp}:${Dest}/"

Write-Host "Stopping assistant service to free UART port..." -ForegroundColor Cyan
ssh -i "$SshKey" -o StrictHostKeyChecking=no "${User}@${HostIp}" "sudo systemctl stop assistant.service"

Write-Host "Ensuring esptool is installed on Tachyon..." -ForegroundColor Cyan
ssh -i "$SshKey" -o StrictHostKeyChecking=no "${User}@${HostIp}" "~/assistant/venv/bin/pip install esptool"

Write-Host "Flashing ESP32 via Tachyon UART..." -ForegroundColor Magenta
Write-Host "========================================================" -ForegroundColor Magenta
Write-Host "NOTE: If the Tachyon is not wired to auto-reset the ESP32," -ForegroundColor Yellow
Write-Host "you may need to hold the BOOT button on the ESP32 right now!" -ForegroundColor Yellow
Write-Host "========================================================" -ForegroundColor Magenta

$FlashCmd = "~/assistant/venv/bin/esptool.py --chip esp32s3 --port $Port --baud 115200 write_flash -z --flash_mode dio --flash_freq 80m --flash_size 8MB 0x0 ${Dest}/bootloader.bin 0x8000 ${Dest}/partitions.bin 0x10000 ${Dest}/firmware.bin"

ssh -i "$SshKey" -t -o StrictHostKeyChecking=no "${User}@${HostIp}" "$FlashCmd"

Write-Host "Restarting assistant service..." -ForegroundColor Cyan
ssh -i "$SshKey" -o StrictHostKeyChecking=no "${User}@${HostIp}" "sudo systemctl start assistant.service"

Write-Host "Done!" -ForegroundColor Green
