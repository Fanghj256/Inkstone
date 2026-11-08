$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*inbox.py*' }

if (-not $procs) {
    Write-Host "No inbox process is running."
    exit
}

foreach ($p in $procs) {
    Stop-Process -Id $p.ProcessId -Force
    Write-Host "Stopped PID $($p.ProcessId)"
}
