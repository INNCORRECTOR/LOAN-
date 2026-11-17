# Start the local MySQL 8.4 server used by this app (no Windows service).
$ini = "C:\Users\X\loan\mysql-data\my.ini"
$mysqld = "C:\Program Files\MySQL\MySQL Server 8.4\bin\mysqld.exe"
$listening = netstat -ano | Select-String ":3306\s+.*LISTENING"
if ($listening) {
    Write-Host "MySQL is already listening on port 3306."
    exit 0
}
if (-not (Test-Path $mysqld)) {
    Write-Error "MySQL Server was not found at $mysqld"
    exit 1
}
Start-Process -FilePath $mysqld -ArgumentList "--defaults-file=`"$ini`"" -WindowStyle Hidden
Write-Host "MySQL starting. Wait a few seconds, then run: python app.py"
