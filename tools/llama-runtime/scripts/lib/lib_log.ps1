function Write-Info {
    param([Parameter(Mandatory=$true)][string]$Message)
    Write-Host "[INFO] $Message"
}

function Write-Warn {
    param([Parameter(Mandatory=$true)][string]$Message)
    Write-Host "[WARN] $Message" -ForegroundColor Yellow
}

function Write-Err {
    param([Parameter(Mandatory=$true)][string]$Message)
    Write-Host "[ERR ] $Message" -ForegroundColor Red
}

function Write-Ok {
    param([Parameter(Mandatory=$true)][string]$Message)
    Write-Host "[OK  ] $Message" -ForegroundColor Green
}

function Write-Run {
    param([Parameter(Mandatory=$true)][string]$Message)
    Write-Host "[RUN ] $Message" -ForegroundColor Cyan
}
