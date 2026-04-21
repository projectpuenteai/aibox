# AIBox Control Panel — single-file WPF window with Start / Pause / Stop.
# Self-elevates on launch so button clicks never trigger extra UAC prompts.
#
# Start  -> runs up_stack.ps1 in a background runspace, streams output to the log.
# Pause  -> runs down_stack.ps1 (stack + hotspot), leaves window open.
# Stop   -> runs down_stack.ps1, closes window.
#
# Status panel polls portal/network-info.json every 3 seconds.

param(
  [switch]$NoElevate
)

$ErrorActionPreference = "Stop"

function Test-IsAdministrator {
  $principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
  return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

$scriptDir     = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtimeDir    = Split-Path -Parent $scriptDir
$toolsDir      = Split-Path -Parent $runtimeDir
$aiboxDir      = Split-Path -Parent $toolsDir
$stackDir      = Join-Path $aiboxDir "stack"
$stackEnvFile  = Join-Path $stackDir ".env"
$netInfoFile   = Join-Path $stackDir "portal\network-info.json"
$upScript      = Join-Path $scriptDir "up_stack.ps1"
$downScript    = Join-Path $scriptDir "down_stack.ps1"
$netInfoScript = Join-Path $scriptDir "get_network_info.ps1"

# Self-elevate
if (-not (Test-IsAdministrator) -and -not $NoElevate) {
  try {
    Start-Process -FilePath "powershell.exe" `
      -ArgumentList @("-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", $MyInvocation.MyCommand.Path, "-NoElevate") `
      -Verb RunAs | Out-Null
    exit 0
  } catch {
    [System.Windows.Forms.MessageBox]::Show("AIBox Control requires Administrator access.`n$($_.Exception.Message)", "AIBox", "OK", "Error") | Out-Null
    exit 1
  }
}

function Read-EnvValue {
  param([string]$Key, [string]$Default = "")
  $val = [System.Environment]::GetEnvironmentVariable($Key)
  if (-not [string]::IsNullOrWhiteSpace($val)) { return $val }
  if (Test-Path $stackEnvFile) {
    $line = Get-Content $stackEnvFile -ErrorAction SilentlyContinue |
      Where-Object { $_ -match "^\s*$Key\s*=" } | Select-Object -First 1
    if ($line) {
      $v = ($line -split "=", 2)[1].Trim().Trim('"').Trim("'")
      if (-not [string]::IsNullOrWhiteSpace($v)) { return $v }
    }
  }
  return $Default
}

$ssid     = Read-EnvValue "HOTSPOT_SSID" "AIBox-Puente"
$key      = Read-EnvValue "HOTSPOT_KEY"  "puente1234"
$hostname = Read-EnvValue "OFFLINE_HOSTNAME" "puente.link"

function Show-FatalBootstrapError {
  param([string]$Message)

  $fullMessage = "AIBox Control could not start.`n`n$Message"
  Write-Error $fullMessage
  try {
    Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
    [System.Windows.Forms.MessageBox]::Show($fullMessage, "AIBox Control", "OK", "Error") | Out-Null
  } catch {
    Write-Error "Could not show bootstrap error dialog: $($_.Exception.Message)"
  }
  exit 1
}

try {
  Add-Type -AssemblyName PresentationFramework -ErrorAction Stop
  Add-Type -AssemblyName PresentationCore -ErrorAction Stop
  Add-Type -AssemblyName WindowsBase -ErrorAction Stop
  Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
} catch {
  Show-FatalBootstrapError "Failed to load required WPF assemblies. $($_.Exception.Message)"
}

[xml]$xaml = @"
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="AIBox - Puente" Height="620" Width="760" WindowStartupLocation="CenterScreen"
        Background="#0f172a" Foreground="#e2e8f0" FontFamily="Segoe UI">
  <Grid Margin="16">
    <Grid.RowDefinitions>
      <RowDefinition Height="Auto"/>
      <RowDefinition Height="Auto"/>
      <RowDefinition Height="Auto"/>
      <RowDefinition Height="*"/>
      <RowDefinition Height="Auto"/>
    </Grid.RowDefinitions>

    <StackPanel Grid.Row="0" Orientation="Horizontal" Margin="0,0,0,12">
      <TextBlock Text="AIBox - Project Puente" FontSize="22" FontWeight="Bold" Foreground="#f8fafc"/>
      <TextBlock Name="StatusBadge" Text="Idle" Margin="16,6,0,0" FontSize="14"
                 Padding="10,3,10,3" Background="#334155" Foreground="#f8fafc"/>
    </StackPanel>

    <StackPanel Grid.Row="1" Orientation="Horizontal" Margin="0,0,0,12">
      <Button Name="BtnStart" Content="Start"  Width="140" Height="44" FontSize="16"
              Background="#16a34a" Foreground="White" BorderThickness="0" Margin="0,0,8,0"/>
      <Button Name="BtnPause" Content="Pause"  Width="140" Height="44" FontSize="16"
              Background="#f59e0b" Foreground="White" BorderThickness="0" Margin="0,0,8,0"/>
      <Button Name="BtnStop"  Content="Stop"   Width="140" Height="44" FontSize="16"
              Background="#dc2626" Foreground="White" BorderThickness="0" Margin="0,0,8,0"/>
      <Button Name="BtnCopy"  Content="Copy connect URL" Width="180" Height="44" FontSize="13"
              Background="#334155" Foreground="White" BorderThickness="0"/>
    </StackPanel>

    <Border Grid.Row="2" Background="#1e293b" Padding="12" Margin="0,0,0,12">
      <StackPanel>
        <TextBlock Text="Status" FontWeight="Bold" Foreground="#93c5fd" Margin="0,0,0,6"/>
        <Grid>
          <Grid.ColumnDefinitions>
            <ColumnDefinition Width="180"/>
            <ColumnDefinition Width="*"/>
          </Grid.ColumnDefinitions>
          <Grid.RowDefinitions>
            <RowDefinition/><RowDefinition/><RowDefinition/><RowDefinition/><RowDefinition/><RowDefinition/>
          </Grid.RowDefinitions>
          <TextBlock Grid.Row="0" Grid.Column="0" Text="Hotspot"   Foreground="#94a3b8"/>
          <TextBlock Grid.Row="0" Grid.Column="1" Name="TxtHotspot" Text="unknown"/>
          <TextBlock Grid.Row="1" Grid.Column="0" Text="SSID"      Foreground="#94a3b8"/>
          <TextBlock Grid.Row="1" Grid.Column="1" Name="TxtSsid"   Text="-"/>
          <TextBlock Grid.Row="2" Grid.Column="0" Text="Password"  Foreground="#94a3b8"/>
          <TextBlock Grid.Row="2" Grid.Column="1" Name="TxtPassword" Text="-"/>
          <TextBlock Grid.Row="3" Grid.Column="0" Text="Client URL" Foreground="#94a3b8"/>
          <TextBlock Grid.Row="3" Grid.Column="1" Name="TxtUrl"    Text="-" Foreground="#67e8f9"/>
          <TextBlock Grid.Row="4" Grid.Column="0" Text="HTTP ready" Foreground="#94a3b8"/>
          <TextBlock Grid.Row="4" Grid.Column="1" Name="TxtHttp"   Text="-"/>
          <TextBlock Grid.Row="5" Grid.Column="0" Text="DNS ready"  Foreground="#94a3b8"/>
          <TextBlock Grid.Row="5" Grid.Column="1" Name="TxtDns"    Text="-"/>
        </Grid>
      </StackPanel>
    </Border>

    <Border Grid.Row="3" Background="#0b1220" Padding="0">
      <TextBox Name="LogBox" IsReadOnly="True" Background="#0b1220" Foreground="#cbd5e1"
               FontFamily="Consolas" FontSize="12" TextWrapping="NoWrap"
               VerticalScrollBarVisibility="Auto" HorizontalScrollBarVisibility="Auto"
               BorderThickness="0" Padding="8"/>
    </Border>

    <TextBlock Grid.Row="4" Name="TxtFooter" Margin="0,8,0,0" FontSize="11" Foreground="#64748b"
               Text="Ready."/>
  </Grid>
</Window>
"@

try {
  $reader = New-Object System.Xml.XmlNodeReader $xaml
} catch {
  Show-FatalBootstrapError "Failed to create the XAML reader. $($_.Exception.Message)"
}

if ($null -eq $reader) {
  Show-FatalBootstrapError "The XAML reader was not created."
}

try {
  $window = [Windows.Markup.XamlReader]::Load($reader)
} catch {
  Show-FatalBootstrapError "Failed to load the WPF window markup. $($_.Exception.Message)"
}

if ($null -eq $window) {
  Show-FatalBootstrapError "The WPF window markup loaded to a null window."
}

$ctrl = @{}
try {
  foreach ($name in @("BtnStart","BtnPause","BtnStop","BtnCopy","LogBox","StatusBadge",
                      "TxtHotspot","TxtSsid","TxtPassword","TxtUrl","TxtHttp","TxtDns","TxtFooter")) {
    $ctrl[$name] = $window.FindName($name)
    if ($null -eq $ctrl[$name]) {
      throw "Control '$name' was not found in the WPF markup."
    }
  }
} catch {
  Show-FatalBootstrapError "Failed while resolving WPF controls. $($_.Exception.Message)"
}

$ctrl.TxtSsid.Text     = $ssid
$ctrl.TxtPassword.Text = $key

$script:busy = $false
$script:connectUrl = "http://$hostname/"
$script:activeJobs = @()
$script:pendingCloseAfterJob = $false
$script:autoStartAttempted = $false

function Write-Log {
  param([string]$Line)
  if ([string]::IsNullOrEmpty($Line)) { return }
  $ts = (Get-Date).ToString("HH:mm:ss")
  $window.Dispatcher.Invoke([action]{
    $ctrl.LogBox.AppendText("[$ts] $Line`r`n")
    $ctrl.LogBox.ScrollToEnd()
  })
}

function Set-Busy {
  param([bool]$On, [string]$Label = "Working")
  $script:busy = $On
  $window.Dispatcher.Invoke([action]{
    $ctrl.BtnStart.IsEnabled = -not $On
    $ctrl.BtnPause.IsEnabled = -not $On
    $ctrl.BtnStop.IsEnabled  = -not $On
    $ctrl.StatusBadge.Text   = if ($On) { $Label } else { "Idle" }
    $ctrl.StatusBadge.Background = if ($On) {
      (New-Object System.Windows.Media.SolidColorBrush ([System.Windows.Media.Color]::FromRgb(245,158,11)))
    } else {
      (New-Object System.Windows.Media.SolidColorBrush ([System.Windows.Media.Color]::FromRgb(51,65,85)))
    }
  })
}

function Get-NetworkInfoSnapshot {
  param([switch]$Refresh)

  if ($Refresh -and (Test-Path $netInfoScript)) {
    try {
      & powershell -ExecutionPolicy Bypass -File $netInfoScript -Quiet | Out-Null
    } catch {
      Write-Log "WARN: get_network_info.ps1 refresh failed: $($_.Exception.Message)"
    }
  }

  if (-not (Test-Path $netInfoFile)) { return $null }
  try {
    return (Get-Content $netInfoFile -Raw -ErrorAction Stop | ConvertFrom-Json)
  } catch {
    Write-Log "WARN: Could not read network-info.json: $($_.Exception.Message)"
    return $null
  }
}

function Test-UiReadyState {
  param($Info)

  if (-not $Info -or -not $Info.hotspot) { return $false }
  $hs = $Info.hotspot
  $httpReady = $false
  if ($hs.validation) {
    $httpReady = [bool]$hs.validation.http_ready
  }
  return ($hs.status -eq "active" -and $httpReady)
}

function Update-Status {
  param($Info = $null)

  if (-not $Info) {
    $Info = Get-NetworkInfoSnapshot
  }
  if (-not $Info) { return $null }

  $window.Dispatcher.Invoke([action]{
    $hs = $Info.hotspot
    if ($hs -and $hs.status -eq "active") {
      $ctrl.TxtHotspot.Text = "active ($($hs.readiness))"
      $ctrl.TxtHotspot.Foreground = (New-Object System.Windows.Media.SolidColorBrush ([System.Windows.Media.Color]::FromRgb(134,239,172)))
    } else {
      $ctrl.TxtHotspot.Text = if ($hs) { [string]$hs.status } else { "off" }
      $ctrl.TxtHotspot.Foreground = (New-Object System.Windows.Media.SolidColorBrush ([System.Windows.Media.Color]::FromRgb(248,113,113)))
    }

    if ($hs -and $hs.ssid)     { $ctrl.TxtSsid.Text = $hs.ssid }
    if ($hs -and $hs.password) { $ctrl.TxtPassword.Text = $hs.password }

    $httpReady = $false; $dnsReady = $false; $hostReady = $false
    if ($hs -and $hs.validation) {
      $httpReady = [bool]$hs.validation.http_ready
      $dnsReady  = [bool]$hs.validation.dns_ready
      $hostReady = [bool]$hs.validation.hostname_ready
    }
    $ctrl.TxtHttp.Text = if ($httpReady) { "yes" } else { "no" }
    $ctrl.TxtDns.Text  = if ($dnsReady)  { "yes" } else { "no" }

    if ($hostReady) {
      $script:connectUrl = "http://$hostname/"
    } elseif ($hs -and $hs.host_ip) {
      $script:connectUrl = "http://$($hs.host_ip)/"
    } else {
      $script:connectUrl = $Info.primary_url
      if (-not $script:connectUrl) { $script:connectUrl = "http://$hostname/" }
    }
    $ctrl.TxtUrl.Text = $script:connectUrl

    if (-not $script:busy) {
      if ($hs -and $hs.status -eq "active" -and $httpReady) {
        $ctrl.StatusBadge.Text = "Ready"
        $ctrl.StatusBadge.Background = (New-Object System.Windows.Media.SolidColorBrush ([System.Windows.Media.Color]::FromRgb(22,163,74)))
      } else {
        $ctrl.StatusBadge.Text = "Offline"
        $ctrl.StatusBadge.Background = (New-Object System.Windows.Media.SolidColorBrush ([System.Windows.Media.Color]::FromRgb(100,116,139)))
      }
    }
  })

  return $Info
}

function Start-BackgroundScript {
  param([string]$Path, [string[]]$ScriptArgs = @(), [string]$Label = "Working")
  if ($script:busy) { return }
  if (-not (Test-Path $Path)) {
    Write-Log "ERROR: script not found: $Path"
    return
  }
  Set-Busy -On $true -Label $Label
  Write-Log "----- $Label : $([System.IO.Path]::GetFileName($Path)) -----"

  $rs = [runspacefactory]::CreateRunspace()
  $rs.ApartmentState = "STA"
  $rs.Open()
  $rs.SessionStateProxy.SetVariable("uiPath", $Path)
  $rs.SessionStateProxy.SetVariable("uiArgs", $ScriptArgs)
  $rs.SessionStateProxy.SetVariable("uiWindow", $window)
  $rs.SessionStateProxy.SetVariable("uiLogBox", $ctrl.LogBox)

  $ps = [powershell]::Create()
  $ps.Runspace = $rs
  [void]$ps.AddScript({
    param()
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = "powershell.exe"
    # Quote script path in case of spaces; append any passthrough args.
    $psi.Arguments = '-ExecutionPolicy Bypass -File "' + $uiPath + '" ' + ($uiArgs -join " ")
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError  = $true
    $psi.CreateNoWindow = $true
    $proc = [System.Diagnostics.Process]::Start($psi)
    $append = {
      param($line, $isErr)
      if ([string]::IsNullOrEmpty($line)) { return }
      $ts = (Get-Date).ToString("HH:mm:ss")
      $prefix = if ($isErr) { "[$ts][err] " } else { "[$ts] " }
      $uiWindow.Dispatcher.Invoke([action]{
        $uiLogBox.AppendText($prefix + $line + "`r`n")
        $uiLogBox.ScrollToEnd()
      })
    }
    while (-not $proc.StandardOutput.EndOfStream) { & $append $proc.StandardOutput.ReadLine() $false }
    while (-not $proc.StandardError.EndOfStream)  { & $append $proc.StandardError.ReadLine()  $true  }
    $proc.WaitForExit()
    return $proc.ExitCode
  })

  $handle = $ps.BeginInvoke()
  $script:activeJobs = @(@{ PS = $ps; Handle = $handle; RS = $rs; Label = $Label })

  # Poll completion on a dispatcher timer so we don't block UI
  $timer = New-Object System.Windows.Threading.DispatcherTimer
  $timer.Interval = [TimeSpan]::FromMilliseconds(300)
  $timer.Add_Tick({
    if ($script:activeJobs[0].Handle.IsCompleted) {
      $timer.Stop()
      try {
        $exit = $script:activeJobs[0].PS.EndInvoke($script:activeJobs[0].Handle)
        Write-Log ("----- {0} finished (exit={1}) -----" -f $script:activeJobs[0].Label, $exit)
      } catch {
        Write-Log ("----- {0} threw: {1} -----" -f $script:activeJobs[0].Label, $_.Exception.Message)
      } finally {
        $script:activeJobs[0].PS.Dispose()
        $script:activeJobs[0].RS.Close()
      }
      Set-Busy -On $false
      Update-Status
      if ($script:pendingCloseAfterJob) {
        $script:pendingCloseAfterJob = $false
        $window.Close()
      }
    }
  })
  $timer.Start()
}

function Start-IfNeededOnLaunch {
  if ($script:autoStartAttempted -or $script:busy) { return }
  $script:autoStartAttempted = $true

  $info = Get-NetworkInfoSnapshot -Refresh
  $null = Update-Status -Info $info

  if (Test-UiReadyState -Info $info) {
    Write-Log "AIBox is already up. Control panel attached without restarting the stack."
    $ctrl.TxtFooter.Text = "AIBox is already running."
    return
  }

  Write-Log "AIBox is not ready. Starting stack and hotspot automatically."
  $ctrl.TxtFooter.Text = "Starting AIBox automatically..."
  Start-BackgroundScript -Path $upScript -ScriptArgs @() -Label "Starting AIBox"
}

function Invoke-StartupSafely {
  try {
    Start-IfNeededOnLaunch
  } catch {
    $message = $_.Exception.Message
    try {
      Write-Log "Startup failed: $message"
    } catch {}
    if ($ctrl.ContainsKey("TxtFooter") -and $ctrl.TxtFooter) {
      $ctrl.TxtFooter.Text = "Startup failed: $message"
    }
  }
}

$ctrl.BtnStart.Add_Click({
  Start-BackgroundScript -Path $upScript -ScriptArgs @() -Label "Starting AIBox"
})
$ctrl.BtnPause.Add_Click({
  Start-BackgroundScript -Path $downScript -ScriptArgs @() -Label "Pausing (stack down)"
})
$ctrl.BtnStop.Add_Click({
  $script:pendingCloseAfterJob = $true
  Start-BackgroundScript -Path $downScript -ScriptArgs @() -Label "Stopping"
})
$ctrl.BtnCopy.Add_Click({
  try {
    [System.Windows.Clipboard]::SetText($script:connectUrl)
    $ctrl.TxtFooter.Text = "Copied: $script:connectUrl"
  } catch {
    $ctrl.TxtFooter.Text = "Copy failed: $($_.Exception.Message)"
  }
})

# Status poll timer (every 3 s)
$pollTimer = New-Object System.Windows.Threading.DispatcherTimer
$pollTimer.Interval = [TimeSpan]::FromSeconds(3)
$pollTimer.Add_Tick({ $null = Update-Status -Info (Get-NetworkInfoSnapshot -Refresh) })
$pollTimer.Start()

$null = Update-Status -Info (Get-NetworkInfoSnapshot -Refresh)
Write-Log "AIBox Control ready. Hotspot SSID: $ssid / Hostname: $hostname"
$window.Add_ContentRendered({
  Invoke-StartupSafely
})

[void]$window.ShowDialog()
