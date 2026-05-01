# Puente host admin console. Local elevated WPF app for the host computer only.
#
# Sections:
#   1. Bootstrap, paths, elevation, fatal handler
#   2. WPF + XAML (portal blue/gradient palette, hero button, dial grid,
#      security/network events console, per-core popup)
#   3. Locale, helpers, dials, updates, background scripts, hotkeys
#   4. Timers + first paint + ShowDialog

param(
  [switch]$NoElevate,
  [switch]$SelfTest,
  [switch]$AdminRequiredSelfTest
)

$ErrorActionPreference = "Stop"

# Earliest possible diagnostic — confirms the script *entered* (vs. powershell.exe
# bailing during arg parsing). Written before paths exist, so use a hard-coded
# fallback dir if needed.
try {
  $_earlyLogDir = "C:\AIBox\aibox\backend-data\appdata\host-admin"
  if (-not (Test-Path $_earlyLogDir)) { New-Item -ItemType Directory -Path $_earlyLogDir -Force | Out-Null }
  $_earlyTs = Get-Date -Format "yyyy-MM-ddTHH:mm:ssK"
  Add-Content -LiteralPath (Join-Path $_earlyLogDir "ui-bootstrap.log") `
    -Value ("$_earlyTs Script entered. PID=$PID Args=`"$($MyInvocation.Line)`" NoElevate=$NoElevate SelfTest=$SelfTest") `
    -Encoding UTF8
} catch {}

# ---- Path resolution -------------------------------------------------------

$scriptDir       = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtimeDir      = Split-Path -Parent $scriptDir
$toolsDir        = Split-Path -Parent $runtimeDir
$aiboxDir        = Split-Path -Parent $toolsDir
$stackDir        = Join-Path $aiboxDir "stack"
$stackEnvFile    = Join-Path $stackDir ".env"
$netInfoFile     = Join-Path $stackDir "portal\network-info.json"
$upScript        = Join-Path $scriptDir "up_stack.ps1"
$downScript      = Join-Path $scriptDir "down_stack.ps1"
$netInfoScript   = Join-Path $scriptDir "get_network_info.ps1"
$metricsScript   = Join-Path $scriptDir "get_system_metrics.ps1"
$caddyAccessLog  = Join-Path $aiboxDir "logs\caddy\access.log"
$logoPath        = Join-Path $stackDir "portal\assets\circlelogo.png"
$bootstrapLogDir = Join-Path $aiboxDir "backend-data\appdata\host-admin"
$bootstrapLogFile= Join-Path $bootstrapLogDir "ui-bootstrap.log"
$prefsFile       = Join-Path $bootstrapLogDir "ui-prefs.json"
$securityDb      = Join-Path $aiboxDir "backend-data\appdata\storage.db"

# ---- Bootstrap log + fatal handler ----------------------------------------

function Write-BootstrapLog {
  param([string]$Message)
  try {
    if (-not (Test-Path $bootstrapLogDir)) {
      New-Item -ItemType Directory -Path $bootstrapLogDir -Force | Out-Null
    }
    $ts = Get-Date -Format "yyyy-MM-ddTHH:mm:ssK"
    Add-Content -LiteralPath $bootstrapLogFile -Value "$ts $Message" -Encoding UTF8
  } catch {}
}

function Show-FatalBootstrapError {
  param([string]$Message)
  Write-BootstrapLog "FATAL $Message"
  $full = "Puente Admin no pudo iniciar.`n`n$Message`n`nLog: $bootstrapLogFile"
  try { Write-Error $full } catch {}
  try {
    Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
    [System.Windows.Forms.MessageBox]::Show($full, "Puente Admin", "OK", "Error") | Out-Null
  } catch {}
  exit 1
}

function Register-DispatcherUnhandledExceptionHandler {
  param(
    [Parameter(Mandatory)] $Dispatcher,
    [switch]$TestOnly
  )
  if ($null -eq $Dispatcher) {
    throw "WPF dispatcher is not available."
  }

  $handler = {
    param($s, $e)
    $msg = ""
    $stack = ""
    try { $msg = $e.Exception.Message } catch {}
    try { if ($e.Exception.StackTrace) { $stack = $e.Exception.StackTrace } } catch {}
    $inner = ""
    try {
      $ex = $e.Exception
      while ($ex -and $ex.InnerException) {
        $ex = $ex.InnerException
        $inner += "`n  inner: " + $ex.GetType().FullName + ": " + $ex.Message
        if ($ex.StackTrace) { $inner += "`n    " + ($ex.StackTrace -replace "`r?`n", "`n    ") }
      }
    } catch {}
    Write-BootstrapLog ("DispatcherUnhandledException: " + $msg + "`nCLRStack:`n  " + ($stack -replace "`r?`n", "`n  ") + $inner)
    $e.Handled = $true
  }

  $Dispatcher.add_UnhandledException($handler)
  if (-not $TestOnly) {
    Write-BootstrapLog "Dispatcher unhandled-exception handler registered."
  }
}

# ---- Admin-required friendly window ----------------------------------------

function Show-AdminRequiredWindow {
  param([string]$Detail = "", [switch]$LoadOnly)
  Write-BootstrapLog "Showing admin-required window. Detail=$Detail"
  try {
    Add-Type -AssemblyName PresentationFramework -ErrorAction Stop
    Add-Type -AssemblyName PresentationCore -ErrorAction Stop
    Add-Type -AssemblyName WindowsBase -ErrorAction Stop
    [xml]$adminXaml = @"
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Consola Puente Admin" Height="360" Width="560" ResizeMode="NoResize"
        WindowStartupLocation="CenterScreen" FontFamily="Aptos, Segoe UI" Foreground="#0f172a">
  <Grid>
    <Grid.Background>
      <LinearGradientBrush StartPoint="0,0" EndPoint="1,1">
        <GradientStop Color="#EAF2FB" Offset="0"/>
        <GradientStop Color="#D6E4F6" Offset="1"/>
      </LinearGradientBrush>
    </Grid.Background>
    <Border Margin="18" CornerRadius="22" BorderBrush="#FFFFFF" BorderThickness="1" Background="#EFFFFFFF" Padding="26">
      <StackPanel VerticalAlignment="Center">
        <TextBlock Text="Consola Puente Admin" FontSize="24" FontWeight="Bold" Foreground="#0F172A" Margin="0,0,0,6"/>
        <TextBlock Text="Se requieren permisos de administrador" FontSize="18" FontWeight="SemiBold" Foreground="#1B4F9C" Margin="0,0,0,14"/>
        <TextBlock Text="Esta aplicacion controla Docker, el hotspot movil y la configuracion de red del equipo host." FontSize="14" Foreground="#475569" TextWrapping="Wrap" Margin="0,0,0,10"/>
        <TextBlock Text="Cierra esta ventana, vuelve a abrir la app desde el escritorio y selecciona Si cuando Windows pida permisos." FontSize="14" Foreground="#475569" TextWrapping="Wrap" Margin="0,0,0,22"/>
        <Button Name="CloseButton" Content="Cerrar" Width="140" Height="42" HorizontalAlignment="Left" Background="#2F74DB" Foreground="White" BorderBrush="#2F74DB" FontWeight="SemiBold"/>
      </StackPanel>
    </Border>
  </Grid>
</Window>
"@
    $reader = New-Object System.Xml.XmlNodeReader $adminXaml
    $adminWindow = [Windows.Markup.XamlReader]::Load($reader)
    $closeButton = $adminWindow.FindName("CloseButton")
    if ($closeButton) { $closeButton.Add_Click({ $adminWindow.Close() }) }
    if ($LoadOnly) {
      Write-Output "OK: admin-required WPF markup loaded."
      return
    }
    [void]$adminWindow.ShowDialog()
  } catch {
    try {
      Add-Type -AssemblyName System.Windows.Forms -ErrorAction SilentlyContinue
      [System.Windows.Forms.MessageBox]::Show(
        "La consola Puente requiere permisos de administrador.`nCierra y vuelve a abrir la app, luego selecciona Si en Windows.",
        "Puente Admin", "OK", "Warning") | Out-Null
    } catch {}
  }
}

if ($AdminRequiredSelfTest) {
  Show-AdminRequiredWindow -Detail "self-test" -LoadOnly
  exit 0
}

# ---- Elevation -------------------------------------------------------------

function Test-IsAdministrator {
  $principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
  return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-IsAdministrator) -and -not $NoElevate -and -not $SelfTest) {
  Write-BootstrapLog "Non-admin launch; requesting elevation."
  $myPath = $MyInvocation.MyCommand.Path
  if (-not (Test-Path $myPath)) {
    Show-FatalBootstrapError "Script path no longer exists: $myPath. Re-run install_autostart.ps1."
  }
  try {
    Start-Process -FilePath "powershell.exe" `
      -ArgumentList @("-STA", "-ExecutionPolicy", "Bypass", "-File", $myPath, "-NoElevate") `
      -Verb RunAs | Out-Null
    Write-BootstrapLog "Elevation request accepted; exiting launcher process."
    exit 0
  } catch {
    Write-BootstrapLog "Elevation request failed or denied: $($_.Exception.Message)"
    Show-AdminRequiredWindow -Detail $_.Exception.Message
    exit 1
  }
}

if (-not (Test-IsAdministrator) -and $NoElevate -and -not $SelfTest) {
  Write-BootstrapLog "NoElevate was supplied but process is not admin; showing admin-required window."
  Show-AdminRequiredWindow -Detail "NoElevate non-admin launch"
  exit 1
}

Write-BootstrapLog "Launching full admin console. IsAdmin=$(Test-IsAdministrator) SelfTest=$SelfTest"

# Anything below this line runs inside a single trap so silent failures stop.
trap {
  Show-FatalBootstrapError ("Unhandled at startup: " + $_.Exception.Message + "`n" + $_.ScriptStackTrace)
}

# ---- Env loader ------------------------------------------------------------

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

Write-BootstrapLog "Env loaded: ssid=$ssid hostname=$hostname"

# ---- Prefs (language, window) ---------------------------------------------

function Read-Prefs {
  if (-not (Test-Path $prefsFile)) { return @{} }
  try {
    $raw = Get-Content -LiteralPath $prefsFile -Raw -ErrorAction Stop
    $obj = $raw | ConvertFrom-Json -ErrorAction Stop
    $h = @{}
    foreach ($p in $obj.PSObject.Properties) { $h[$p.Name] = $p.Value }
    return $h
  } catch { return @{} }
}

function Save-Prefs {
  param([hashtable]$Data)
  try {
    if (-not (Test-Path $bootstrapLogDir)) {
      New-Item -ItemType Directory -Path $bootstrapLogDir -Force | Out-Null
    }
    ($Data | ConvertTo-Json -Depth 4) | Set-Content -LiteralPath $prefsFile -Encoding UTF8
  } catch {}
}

# ---- Load WPF --------------------------------------------------------------

try {
  Add-Type -AssemblyName PresentationFramework -ErrorAction Stop
  Add-Type -AssemblyName PresentationCore -ErrorAction Stop
  Add-Type -AssemblyName WindowsBase -ErrorAction Stop
  Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
  Add-Type -AssemblyName System.Data -ErrorAction SilentlyContinue
} catch {
  Show-FatalBootstrapError "No se pudieron cargar las librerias WPF. $($_.Exception.Message)"
}
Write-BootstrapLog "WPF assemblies loaded"

# Dot-source metrics script so we can call its helpers in-process instead of spawning
# powershell.exe on every metrics tick. Standalone-script behavior is preserved by the
# `$MyInvocation.InvocationName -ne '.'` guard at the bottom of get_system_metrics.ps1.
try {
  . $metricsScript
  Write-BootstrapLog "Metrics helpers dot-sourced"
} catch {
  Write-BootstrapLog "Metrics dot-source failed: $($_.Exception.Message)"
}

# ---- XAML ------------------------------------------------------------------

[xml]$xaml = @"
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Consola Puente Admin" Height="860" Width="1280" MinHeight="780" MinWidth="1140"
        WindowStartupLocation="CenterScreen" FontFamily="Aptos, Segoe UI, Tahoma" Foreground="#0F172A">
  <Window.Resources>
    <SolidColorBrush x:Key="Accent" Color="#2F74DB"/>
    <SolidColorBrush x:Key="AccentStrong" Color="#1B4F9C"/>
    <SolidColorBrush x:Key="AccentSoft" Color="#EEF4FB"/>
    <SolidColorBrush x:Key="PanelBrush" Color="#FFFFFF"/>
    <SolidColorBrush x:Key="PanelSoft" Color="#F8FBFF"/>
    <SolidColorBrush x:Key="PanelBorder" Color="#D9E5F3"/>
    <SolidColorBrush x:Key="TextSoft" Color="#475569"/>
    <SolidColorBrush x:Key="TextMuted" Color="#64748B"/>
    <SolidColorBrush x:Key="Success" Color="#059669"/>
    <SolidColorBrush x:Key="Warn" Color="#D97706"/>
    <SolidColorBrush x:Key="Danger" Color="#DC2626"/>

    <Style TargetType="Button" x:Key="GhostBtn">
      <Setter Property="MinHeight" Value="40"/>
      <Setter Property="Padding" Value="14,0"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="BorderBrush" Value="#D9E5F3"/>
      <Setter Property="Background" Value="#FFFFFF"/>
      <Setter Property="Foreground" Value="#1B4F9C"/>
      <Setter Property="Cursor" Value="Hand"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="Button">
            <Border x:Name="bd" CornerRadius="14" Background="{TemplateBinding Background}"
                    BorderBrush="{TemplateBinding BorderBrush}" BorderThickness="{TemplateBinding BorderThickness}">
              <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center" Margin="{TemplateBinding Padding}"/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter TargetName="bd" Property="Background" Value="#EEF4FB"/>
              </Trigger>
              <Trigger Property="IsEnabled" Value="False">
                <Setter Property="Opacity" Value="0.55"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>

    <Style x:Key="MetricLabel" TargetType="TextBlock">
      <Setter Property="FontSize" Value="11"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="Foreground" Value="#64748B"/>
    </Style>
    <Style x:Key="SectionTitle" TargetType="TextBlock">
      <Setter Property="FontSize" Value="14"/>
      <Setter Property="FontWeight" Value="Bold"/>
      <Setter Property="Foreground" Value="#1B4F9C"/>
      <Setter Property="Margin" Value="0,0,0,8"/>
    </Style>

    <Style TargetType="Border" x:Key="Card">
      <Setter Property="CornerRadius" Value="22"/>
      <Setter Property="Background" Value="#FFFFFF"/>
      <Setter Property="BorderBrush" Value="#D9E5F3"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="Padding" Value="18"/>
      <Setter Property="Effect">
        <Setter.Value>
          <DropShadowEffect BlurRadius="28" ShadowDepth="0" Color="#1B4F8A" Opacity="0.10"/>
        </Setter.Value>
      </Setter>
    </Style>
  </Window.Resources>

  <Grid>
    <Grid.Background>
      <LinearGradientBrush StartPoint="0,0" EndPoint="1,1">
        <GradientStop Color="#EAF2FB" Offset="0"/>
        <GradientStop Color="#E2EDF9" Offset="0.5"/>
        <GradientStop Color="#D6E4F6" Offset="1"/>
      </LinearGradientBrush>
    </Grid.Background>

    <Grid Margin="22">
      <Grid.RowDefinitions>
        <RowDefinition Height="Auto"/>
        <RowDefinition Height="Auto"/>
        <RowDefinition Height="Auto"/>
        <RowDefinition Height="*"/>
        <RowDefinition Height="Auto"/>
      </Grid.RowDefinitions>

      <!-- Top bar -->
      <Grid Grid.Row="0" Margin="4,0,4,16">
        <Grid.ColumnDefinitions>
          <ColumnDefinition Width="*"/>
          <ColumnDefinition Width="Auto"/>
        </Grid.ColumnDefinitions>
        <StackPanel Orientation="Horizontal">
          <Border Width="56" Height="56" CornerRadius="28" Background="#EDF4FF" Margin="0,0,16,0">
            <Image Name="LogoImage" Stretch="UniformToFill"/>
          </Border>
          <StackPanel VerticalAlignment="Center">
            <TextBlock Name="TxtTitle" FontSize="26" FontWeight="Bold" Foreground="#0F172A"/>
            <TextBlock Name="TxtSubtitle" FontSize="13" Foreground="#475569" Margin="0,2,0,0"/>
          </StackPanel>
          <Border Name="StatusBadge" Height="34" CornerRadius="17" Padding="14,5" Background="#E2E8F0" Margin="18,11,0,0">
            <StackPanel Orientation="Horizontal" VerticalAlignment="Center">
              <Ellipse Name="StatusDot" Width="10" Height="10" Fill="#94A3B8" Margin="0,0,8,0"/>
              <TextBlock Name="TxtStatusBadge" FontSize="12" FontWeight="SemiBold" Foreground="#334155"/>
            </StackPanel>
          </Border>
        </StackPanel>
        <StackPanel Grid.Column="1" Orientation="Horizontal" VerticalAlignment="Center">
          <Button Name="BtnCopy" Style="{StaticResource GhostBtn}" Margin="0,0,10,0"/>
          <Button Name="BtnLang" Style="{StaticResource GhostBtn}" Width="110"/>
        </StackPanel>
      </Grid>

      <!-- Hero card -->
      <Border Grid.Row="1" Style="{StaticResource Card}" Margin="0,0,0,14">
        <Grid>
          <Grid.ColumnDefinitions>
            <ColumnDefinition Width="*"/>
            <ColumnDefinition Width="Auto"/>
          </Grid.ColumnDefinitions>
          <StackPanel Margin="6,4,0,0">
            <TextBlock Name="TxtHeroLabel" FontSize="13" FontWeight="SemiBold" Foreground="#2F74DB"/>
            <TextBlock Name="TxtMainStatus" FontSize="38" FontWeight="Bold" Foreground="#0F172A" Margin="0,4,0,2"/>
            <StackPanel Orientation="Horizontal" Margin="0,2,0,0">
              <TextBlock Name="TxtConnectUrl" FontSize="16" Foreground="#1B4F9C" FontFamily="Cascadia Code, Consolas" Margin="0,0,12,0"/>
              <Border Background="#EEF4FB" CornerRadius="10" Padding="10,4">
                <TextBlock Name="TxtSsidInline" FontSize="12" Foreground="#1B4F9C" FontWeight="SemiBold"/>
              </Border>
            </StackPanel>
            <TextBlock Name="TxtStatusHint" FontSize="12" Foreground="#64748B" TextWrapping="Wrap" Margin="0,10,0,0"/>
          </StackPanel>
          <StackPanel Grid.Column="1" Width="280" VerticalAlignment="Center">
            <Button Name="BtnPrimary" Height="76" FontSize="20" FontWeight="Bold" Foreground="White" BorderThickness="0" Cursor="Hand">
              <Button.Template>
                <ControlTemplate TargetType="Button">
                  <Border x:Name="heroBd" CornerRadius="22">
                    <Border.Background>
                      <LinearGradientBrush StartPoint="0,0" EndPoint="1,0">
                        <GradientStop Color="#4B84E4" Offset="0"/>
                        <GradientStop Color="#2F74DB" Offset="1"/>
                      </LinearGradientBrush>
                    </Border.Background>
                    <Border.Effect>
                      <DropShadowEffect BlurRadius="22" ShadowDepth="0" Color="#1B4F9C" Opacity="0.45"/>
                    </Border.Effect>
                    <Grid>
                      <Path Name="HeroSpinner" Stroke="#FFFFFF" StrokeThickness="3" Width="28" Height="28"
                            HorizontalAlignment="Left" Margin="22,0,0,0" VerticalAlignment="Center" Visibility="Collapsed"
                            Data="M 14 2 A 12 12 0 1 1 5.5 5.5">
                        <Path.RenderTransform>
                          <RotateTransform x:Name="SpinnerRot" Angle="0" CenterX="14" CenterY="14"/>
                        </Path.RenderTransform>
                      </Path>
                      <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
                    </Grid>
                  </Border>
                  <ControlTemplate.Triggers>
                    <Trigger Property="IsEnabled" Value="False">
                      <Setter Property="Opacity" Value="0.85"/>
                    </Trigger>
                    <Trigger Property="IsMouseOver" Value="True">
                      <Setter TargetName="heroBd" Property="Effect">
                        <Setter.Value>
                          <DropShadowEffect BlurRadius="32" ShadowDepth="0" Color="#1B4F9C" Opacity="0.65"/>
                        </Setter.Value>
                      </Setter>
                    </Trigger>
                  </ControlTemplate.Triggers>
                </ControlTemplate>
              </Button.Template>
            </Button>
            <ProgressBar Name="StartProgress" Height="6" Margin="0,12,0,0" IsIndeterminate="True" Visibility="Collapsed"/>
          </StackPanel>
        </Grid>
      </Border>

      <!-- Hotspot + dial grid -->
      <Grid Grid.Row="2" Margin="0,0,0,14">
        <Grid.ColumnDefinitions>
          <ColumnDefinition Width="360"/>
          <ColumnDefinition Width="*"/>
        </Grid.ColumnDefinitions>

        <Border Grid.Column="0" Style="{StaticResource Card}" Margin="0,0,14,0">
          <StackPanel>
            <TextBlock Name="TxtHotspotTitle" Style="{StaticResource SectionTitle}"/>
            <Grid>
              <Grid.ColumnDefinitions>
                <ColumnDefinition Width="100"/>
                <ColumnDefinition Width="*"/>
              </Grid.ColumnDefinitions>
              <Grid.RowDefinitions>
                <RowDefinition Height="Auto"/>
                <RowDefinition Height="Auto"/>
                <RowDefinition Height="Auto"/>
                <RowDefinition Height="Auto"/>
                <RowDefinition Height="Auto"/>
              </Grid.RowDefinitions>
              <TextBlock Name="LblSsid" Grid.Row="0" Grid.Column="0" Foreground="#64748B" Margin="0,0,0,9"/>
              <TextBlock Name="TxtSsid" Grid.Row="0" Grid.Column="1" FontWeight="SemiBold" TextTrimming="CharacterEllipsis" FontFamily="Cascadia Code, Consolas"/>
              <TextBlock Name="LblPassword" Grid.Row="1" Grid.Column="0" Foreground="#64748B" Margin="0,0,0,9"/>
              <TextBlock Name="TxtPassword" Grid.Row="1" Grid.Column="1" FontWeight="SemiBold" TextTrimming="CharacterEllipsis" FontFamily="Cascadia Code, Consolas"/>
              <TextBlock Name="LblSource" Grid.Row="2" Grid.Column="0" Foreground="#64748B" Margin="0,0,0,9"/>
              <TextBlock Name="TxtSource" Grid.Row="2" Grid.Column="1" TextTrimming="CharacterEllipsis"/>
              <TextBlock Name="LblChecks" Grid.Row="3" Grid.Column="0" Foreground="#64748B" Margin="0,0,0,9"/>
              <TextBlock Name="TxtChecks" Grid.Row="3" Grid.Column="1" TextTrimming="CharacterEllipsis"/>
              <TextBlock Name="LblDevices" Grid.Row="4" Grid.Column="0" Foreground="#64748B"/>
              <TextBlock Name="TxtDevices" Grid.Row="4" Grid.Column="1" FontWeight="SemiBold"/>
            </Grid>
            <Border Margin="0,12,0,0" Background="#F1F5FB" CornerRadius="10" Padding="10,7">
              <StackPanel Orientation="Horizontal">
                <Ellipse Name="DockerDot" Width="9" Height="9" Fill="#94A3B8" Margin="0,2,8,0"/>
                <TextBlock Name="TxtDockerLine" FontSize="12" Foreground="#475569"/>
              </StackPanel>
            </Border>
          </StackPanel>
        </Border>

        <Border Grid.Column="1" Style="{StaticResource Card}">
          <StackPanel>
            <TextBlock Name="TxtPerfTitle" Style="{StaticResource SectionTitle}"/>
            <UniformGrid Name="DialGrid" Rows="1" Columns="6"/>
          </StackPanel>
        </Border>
      </Grid>

      <!-- Consoles row -->
      <Grid Grid.Row="3">
        <Grid.ColumnDefinitions>
          <ColumnDefinition Width="*"/>
          <ColumnDefinition Width="*"/>
        </Grid.ColumnDefinitions>

        <Border Name="CommandPanel" Grid.Column="0" CornerRadius="18" Background="#0B1220" BorderBrush="#1E293B" BorderThickness="1" Margin="0,0,7,0">
          <DockPanel>
            <Border DockPanel.Dock="Top" Background="#111C31" Padding="14,10" CornerRadius="18,18,0,0">
              <Grid>
                <Grid.ColumnDefinitions>
                  <ColumnDefinition Width="*"/>
                  <ColumnDefinition Width="Auto"/>
                </Grid.ColumnDefinitions>
                <TextBlock Name="TxtCommandTitle" Foreground="#BFDBFE" FontWeight="Bold"/>
                <TextBlock Grid.Column="1" Name="TxtCommandHint" Foreground="#64748B" FontSize="11"/>
              </Grid>
            </Border>
            <TextBox Name="CommandLogBox" IsReadOnly="True" Background="#0B1220" Foreground="#CBD5E1"
                     FontFamily="Cascadia Code, Consolas" FontSize="12" TextWrapping="NoWrap"
                     VerticalScrollBarVisibility="Auto" HorizontalScrollBarVisibility="Auto" BorderThickness="0" Padding="12"/>
          </DockPanel>
        </Border>

        <Border Grid.Column="1" CornerRadius="18" Background="#0B1220" BorderBrush="#1E293B" BorderThickness="1" Margin="7,0,0,0">
          <DockPanel>
            <Border DockPanel.Dock="Top" Background="#111C31" Padding="14,10" CornerRadius="18,18,0,0">
              <TextBlock Name="TxtEventsTitle" Foreground="#BFDBFE" FontWeight="Bold"/>
            </Border>
            <Grid>
              <Grid.RowDefinitions>
                <RowDefinition Height="*"/>
                <RowDefinition Height="*"/>
              </Grid.RowDefinitions>
              <ListBox Name="SecurityList" Grid.Row="0" Background="#0B1220" Foreground="#CBD5E1"
                       FontFamily="Cascadia Code, Consolas" FontSize="12" BorderThickness="0,0,0,1" BorderBrush="#1E293B"
                       ScrollViewer.HorizontalScrollBarVisibility="Auto"/>
              <ListBox Name="RequestList" Grid.Row="1" Background="#0B1220" Foreground="#CBD5E1"
                       FontFamily="Cascadia Code, Consolas" FontSize="12" BorderThickness="0"
                       ScrollViewer.HorizontalScrollBarVisibility="Auto"/>
            </Grid>
          </DockPanel>
        </Border>
      </Grid>

      <!-- Footer -->
      <Grid Grid.Row="4" Margin="6,12,6,0">
        <Grid.ColumnDefinitions>
          <ColumnDefinition Width="*"/>
          <ColumnDefinition Width="Auto"/>
        </Grid.ColumnDefinitions>
        <TextBlock Name="TxtFooter" FontSize="12" Foreground="#64748B"/>
        <TextBlock Grid.Column="1" Name="TxtHotkeyHint" FontSize="11" Foreground="#94A3B8"/>
      </Grid>
    </Grid>

    <!-- Per-core CPU popup -->
    <Popup Name="CorePopup" Placement="MousePoint" StaysOpen="False" AllowsTransparency="True" PopupAnimation="Fade">
      <Border CornerRadius="18" Background="#FFFFFF" BorderBrush="#D9E5F3" BorderThickness="1" Padding="18">
        <Border.Effect>
          <DropShadowEffect BlurRadius="32" ShadowDepth="0" Color="#1B4F8A" Opacity="0.18"/>
        </Border.Effect>
        <StackPanel>
          <TextBlock Name="TxtCoreTitle" Style="{StaticResource SectionTitle}"/>
          <WrapPanel Name="CoreWrap" MaxWidth="540"/>
        </StackPanel>
      </Border>
    </Popup>
  </Grid>
</Window>
"@

try {
  $reader = New-Object System.Xml.XmlNodeReader $xaml
  $window = [Windows.Markup.XamlReader]::Load($reader)
} catch {
  Show-FatalBootstrapError "No se pudo cargar la interfaz. $($_.Exception.Message)"
}
Write-BootstrapLog "XAML loaded"

# ---- Controls dictionary ---------------------------------------------------

$controlNames = @(
  "LogoImage","StatusBadge","StatusDot","TxtStatusBadge","BtnCopy","BtnLang","TxtTitle","TxtSubtitle",
  "TxtHeroLabel","TxtMainStatus","TxtConnectUrl","TxtSsidInline","TxtStatusHint","BtnPrimary","StartProgress",
  "TxtHotspotTitle","LblSsid","TxtSsid","LblPassword","TxtPassword","LblSource","TxtSource",
  "LblChecks","TxtChecks","LblDevices","TxtDevices","DockerDot","TxtDockerLine",
  "TxtPerfTitle","DialGrid",
  "CommandPanel","TxtCommandTitle","TxtCommandHint","CommandLogBox",
  "TxtEventsTitle","SecurityList","RequestList",
  "TxtFooter","TxtHotkeyHint",
  "CorePopup","TxtCoreTitle","CoreWrap"
)
$ctrl = @{}
foreach ($name in $controlNames) {
  $ctrl[$name] = $window.FindName($name)
  if ($null -eq $ctrl[$name]) { Show-FatalBootstrapError "No se encontro el control '$name'." }
}

if ($SelfTest) {
  Register-DispatcherUnhandledExceptionHandler -Dispatcher $window.Dispatcher -TestOnly
  Write-Output "OK: WPF markup and controls loaded."
  Write-Output "OK: dispatcher exception handler registered."
  Write-BootstrapLog "Self-test passed."
  exit 0
}

# ---- Logo ------------------------------------------------------------------

if (Test-Path $logoPath) {
  try {
    $image = New-Object System.Windows.Media.Imaging.BitmapImage
    $image.BeginInit()
    $image.UriSource = New-Object System.Uri($logoPath)
    $image.CacheOption = [System.Windows.Media.Imaging.BitmapCacheOption]::OnLoad
    $image.EndInit()
    $ctrl.LogoImage.Source = $image
  } catch {
    Write-BootstrapLog "Logo load failed: $($_.Exception.Message)"
  }
}

# ---- State + locale --------------------------------------------------------

$prefs = Read-Prefs
$script:language    = if ($prefs.ContainsKey("language") -and $prefs.language) { [string]$prefs.language } else { "es" }
$script:busy        = $false
$script:lastReady   = $false
$script:connectUrl  = "http://$hostname/"
$script:activeJobs  = @()
$script:lastRequestFingerprint = ""
$script:lastSecurityFingerprint = ""
$script:dialCtrls   = @{}
$script:dialHistory = @{}
$script:lastCpuValue = 0
$script:perCoreVisible = $false
$script:firstShowDone = $false

if ($prefs.ContainsKey("windowWidth") -and $prefs.windowWidth) {
  try { $window.Width = [double]$prefs.windowWidth } catch {}
}
if ($prefs.ContainsKey("windowHeight") -and $prefs.windowHeight) {
  try { $window.Height = [double]$prefs.windowHeight } catch {}
}

$Text = @{
  es = @{
    title = "Consola Puente Admin"
    subtitle = "Control local del equipo host"
    lang = "English"
    copy = "Copiar URL"
    hero = "Estado del sistema"
    ready = "Encendido"
    off = "Apagado"
    starting = "Iniciando..."
    stopping = "Deteniendo..."
    start = "Iniciar sistema"
    stop = "Detener sistema"
    hotspot = "Hotspot movil"
    perf = "Rendimiento del sistema"
    ssid = "Red"
    password = "Clave"
    source = "Fuente"
    checks = "Pruebas"
    devices = "Dispositivos"
    cpu = "CPU"
    gpu = "GPU"
    gputemp = "Temp GPU"
    gpuclk = "Reloj GPU"
    netUp = "Subida"
    netDown = "Bajada"
    docker = "Docker"
    network = "Red"
    commands = "Consola de arranque"
    events = "Eventos de seguridad y red"
    secEmpty = "Sin eventos de seguridad."
    secMissing = "Base de datos de seguridad no disponible."
    idleHint = "Presiona Iniciar para abrir Docker, levantar los servicios y encender el hotspot."
    readyHint = "Los servicios estan listos para dispositivos conectados."
    startingHint = "Ejecutando comandos de arranque. El hotspot puede tardar un momento en aparecer."
    stoppingHint = "Apagando contenedores y hotspot. Espera unos segundos."
    copied = "URL copiada"
    noRequests = "Sin solicitudes todavia."
    requestMissing = "Esperando el archivo de acceso de Caddy..."
    dockerOn = "Docker activo"
    dockerOff = "Docker apagado"
    unknown = "desconocido"
    yes = "si"
    no = "no"
    coreTitle = "Uso por nucleo de CPU"
    hotkey = "Ctrl+L limpia consola, Ctrl+E enfoca eventos, Ctrl+Q sale."
    cmdHint = "Salida de up_stack.ps1 / down_stack.ps1"
  }
  en = @{
    title = "Puente Admin Console"
    subtitle = "Local host computer control"
    lang = "Espanol"
    copy = "Copy URL"
    hero = "System status"
    ready = "On"
    off = "Off"
    starting = "Starting..."
    stopping = "Stopping..."
    start = "Start system"
    stop = "Stop system"
    hotspot = "Mobile hotspot"
    perf = "System performance"
    ssid = "SSID"
    password = "Password"
    source = "Source"
    checks = "Checks"
    devices = "Devices"
    cpu = "CPU"
    gpu = "GPU"
    gputemp = "GPU temp"
    gpuclk = "GPU clock"
    netUp = "Upload"
    netDown = "Download"
    docker = "Docker"
    network = "Network"
    commands = "Startup console"
    events = "Security and network events"
    secEmpty = "No security events."
    secMissing = "Security database unavailable."
    idleHint = "Press Start to open Docker, start services, and turn on the hotspot."
    readyHint = "Services are ready for connected devices."
    startingHint = "Startup commands are running. The hotspot can take a moment to appear."
    stoppingHint = "Shutting down containers and hotspot. Wait a few seconds."
    copied = "Copied URL"
    noRequests = "No requests yet."
    requestMissing = "Waiting for the Caddy access log..."
    dockerOn = "Docker active"
    dockerOff = "Docker off"
    unknown = "unknown"
    yes = "yes"
    no = "no"
    coreTitle = "CPU usage by core"
    hotkey = "Ctrl+L clears console, Ctrl+E focuses events, Ctrl+Q quits."
    cmdHint = "Output of up_stack.ps1 / down_stack.ps1"
  }
}

if (-not $Text.ContainsKey($script:language)) {
  Write-BootstrapLog "Unsupported UI language '$script:language' in prefs; defaulting to es."
  $script:language = "es"
}

function T { param([string]$Key) return $Text[$script:language][$Key] }

# ---- Brushes + helpers -----------------------------------------------------

function New-Brush {
  param([byte]$R, [byte]$G, [byte]$B)
  return New-Object System.Windows.Media.SolidColorBrush ([System.Windows.Media.Color]::FromRgb($R, $G, $B))
}

function Format-BytesPerSecond {
  param($Value)
  if ($null -eq $Value) { return "-" }
  $n = [double]$Value
  if ($n -ge 1048576) { return ("{0:N1} MB/s" -f ($n / 1048576)) }
  if ($n -ge 1024) { return ("{0:N1} KB/s" -f ($n / 1024)) }
  return ("{0:N0} B/s" -f $n)
}

# ---- Background workers ---------------------------------------------------
# Each Invoke-Background submission gets its own short-lived runspace. A single
# persistent runspace can only run one pipeline at a time, and we have four
# concurrent timers, so single-runspace produces "Pipelines cannot be run
# concurrently" errors. Fresh-per-call costs ~10-50ms vs the ~700ms powershell.exe
# child-process startup we eliminated, and avoids all coordination problems.

$script:bgInFlight = @{}

# Helper-function source that gets prepended to every background work scriptblock.
# Defined as a string so we can splice it onto $Work via [scriptblock]::Create.
$script:bgHelpersSource = @'
function bgTest-Sqlite3Available {
  return ($null -ne (Get-Command sqlite3.exe -ErrorAction SilentlyContinue))
}

function bgGet-DockerReady {
  $saved = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try { $null = & docker info 2>&1; return ($LASTEXITCODE -eq 0) }
  catch { return $false }
  finally { $ErrorActionPreference = $saved }
}

function bgGet-NetworkInfo {
  param([bool]$Refresh, [string]$Script, [string]$File)
  if ($Refresh -and (Test-Path $Script)) {
    try { & powershell -ExecutionPolicy Bypass -File $Script -Quiet | Out-Null } catch {}
  }
  if (-not (Test-Path $File)) { return $null }
  try { return (Get-Content $File -Raw -ErrorAction Stop | ConvertFrom-Json) }
  catch { return $null }
}

function bgGet-RequestRows {
  param([string]$LogPath, [string]$RequestMissingText, [string]$NoRequestsText)
  if (-not (Test-Path $LogPath)) {
    return ,@(@{ Text = $RequestMissingText; Severity = "info" })
  }
  $rows = @()
  try {
    foreach ($line in (Get-Content -LiteralPath $LogPath -Tail 80 -ErrorAction Stop)) {
      if ([string]::IsNullOrWhiteSpace($line)) { continue }
      try {
        $entry = $line | ConvertFrom-Json
        if ($entry.ts) {
          $ts = ([DateTimeOffset]::FromUnixTimeSeconds([int64]$entry.ts)).LocalDateTime.ToString("HH:mm:ss")
        } else {
          $ts = (Get-Date).ToString("HH:mm:ss")
        }
        $remote = "-"; $method = "-"; $uri = "-"
        if ($entry.request) {
          $remote = [string]$entry.request.remote_ip
          $method = [string]$entry.request.method
          $uri = [string]$entry.request.uri
        }
        if ([string]::IsNullOrWhiteSpace($uri)) { $uri = "/" }
        $pathOnly = ($uri -split "\?", 2)[0]
        $status = "-"
        if ($entry.status) { $status = [string]$entry.status }
        $sev = "info"
        $statusInt = 0
        try { $statusInt = [int]$status } catch {}
        if ($statusInt -ge 500) { $sev = "error" }
        elseif ($statusInt -ge 400) { $sev = "warn" }
        $rows += @{
          Text = ("[{0}] {1} {2} {3} -> {4}" -f $ts, $remote, $method, $pathOnly, $status)
          Severity = $sev
        }
      } catch {}
    }
  } catch {
    return ,@(@{ Text = $RequestMissingText; Severity = "info" })
  }
  if ($rows.Count -eq 0) { return ,@(@{ Text = $NoRequestsText; Severity = "info" }) }
  return ,@($rows | Select-Object -Last 60)
}

function bgGet-SecurityRows {
  param([string]$DbPath, [string]$MissingText, [string]$EmptyText)
  if (-not (Test-Path $DbPath)) { return ,@(@{ Text = $MissingText; Severity = "info" }) }
  if (-not (bgTest-Sqlite3Available)) {
    return ,@(@{ Text = $MissingText + " (sqlite3.exe)"; Severity = "info" })
  }
  $rows = @()
  try {
    $sql = "SELECT created_at, severity, event_type, COALESCE(username,'-'), COALESCE(ip,'-'), COALESCE(detail,'') FROM security_events ORDER BY created_ts DESC LIMIT 60;"
    $output = & sqlite3.exe -readonly -separator "`t" $DbPath $sql 2>$null
    foreach ($line in $output) {
      if ([string]::IsNullOrWhiteSpace($line)) { continue }
      $parts = $line -split "`t", 6
      if ($parts.Count -lt 5) { continue }
      $when = $parts[0]; $sev = $parts[1]; $evt = $parts[2]; $user = $parts[3]; $ip = $parts[4]
      if ($parts.Count -ge 6) { $det = $parts[5] } else { $det = "" }
      try { $dt = [DateTime]::Parse($when); $when = $dt.ToString("HH:mm:ss") } catch {}
      $sevKey = "info"
      switch -Wildcard ($sev.ToLower()) {
        "*crit*"  { $sevKey = "error" }
        "*high*"  { $sevKey = "error" }
        "*error*" { $sevKey = "error" }
        "*warn*"  { $sevKey = "warn" }
        default   { $sevKey = "info" }
      }
      $text = ("[{0}] {1,-5} {2} user={3} ip={4} {5}" -f $when, $sev, $evt, $user, $ip, $det)
      $rows += @{ Text = $text; Severity = $sevKey }
    }
  } catch {
    return ,@(@{ Text = "Security read error: $($_.Exception.Message)"; Severity = "warn" })
  }
  if ($rows.Count -eq 0) { return ,@(@{ Text = $EmptyText; Severity = "info" }) }
  return ,$rows
}
'@

# Submit a script block to a fresh background runspace; on completion, marshal the
# result to the UI thread via the dispatcher. Drops overlapping submissions per key.
function Invoke-Background {
  param(
    [Parameter(Mandatory)] [string]$Key,
    [Parameter(Mandatory)] [scriptblock]$Work,
    [Parameter(Mandatory)] [scriptblock]$OnResult,
    [object[]]$Arguments = @()
  )
  if ($script:bgInFlight[$Key]) { return }
  $script:bgInFlight[$Key] = $true

  $rs = $null
  $ps = $null
  try {
    $rs = [runspacefactory]::CreateRunspace()
    $rs.ApartmentState = "STA"
    $rs.Open()
    if ($metricsScript) { $rs.SessionStateProxy.SetVariable("metricsScriptPath", $metricsScript) }

    # Combine helpers + the caller's work into one scriptblock.
    $combined = [scriptblock]::Create($script:bgHelpersSource + "`n" + $Work.ToString())

    $ps = [powershell]::Create()
    $ps.Runspace = $rs
    [void]$ps.AddScript($combined)
    foreach ($arg in $Arguments) { [void]$ps.AddArgument($arg) }
  } catch {
    Write-BootstrapLog "Invoke-Background[$Key] setup failed: $($_.Exception.Message)"
    try { if ($ps) { $ps.Dispose() } } catch {}
    try { if ($rs) { $rs.Close(); $rs.Dispose() } } catch {}
    $script:bgInFlight[$Key] = $false
    return
  }

  $handle = $null
  try {
    $handle = $ps.BeginInvoke()
  } catch {
    Write-BootstrapLog "Invoke-Background[$Key] BeginInvoke failed: $($_.Exception.Message)"
    try { $ps.Dispose() } catch {}
    try { $rs.Close(); $rs.Dispose() } catch {}
    $script:bgInFlight[$Key] = $false
    return
  }

  $timer = New-Object System.Windows.Threading.DispatcherTimer
  $timer.Interval = [TimeSpan]::FromMilliseconds(120)
  $timer.Add_Tick({
    if (-not $handle.IsCompleted) { return }
    $timer.Stop()
    $value = $null
    try {
      $output = $ps.EndInvoke($handle)
      if ($null -ne $output -and $output.Count -gt 0) { $value = $output[$output.Count - 1] }
    } catch {
      $st = $_.ScriptStackTrace; if (-not $st) { $st = "" }
      Write-BootstrapLog "Invoke-Background[$Key] error: $($_.Exception.Message)`n  $st"
    } finally {
      try { $ps.Dispose() } catch {}
      try { $rs.Close(); $rs.Dispose() } catch {}
      $script:bgInFlight[$Key] = $false
    }
    try { & $OnResult $value } catch {
      $st = $_.ScriptStackTrace; if (-not $st) { $st = "" }
      Write-BootstrapLog "Invoke-Background[$Key] callback error: $($_.Exception.Message)`n  $st"
    }
  }.GetNewClosure())
  $timer.Start()
}

function Test-DockerDaemonLocalRaw {
  $saved = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    $null = & docker info 2>&1
    return ($LASTEXITCODE -eq 0)
  } catch {
    return $false
  } finally {
    $ErrorActionPreference = $saved
  }
}

# `docker info` takes 200-500ms+; cache the result so we never call it from the UI thread
# more than once per 15s. The background runspace refreshes this entry.
$script:dockerCache = @{ At = [DateTime]::MinValue; Ready = $false }

function Test-DockerDaemonLocal {
  param([switch]$Force)
  $now = Get-Date
  if (-not $Force -and ($now - $script:dockerCache.At).TotalSeconds -lt 15) {
    return [bool]$script:dockerCache.Ready
  }
  $r = Test-DockerDaemonLocalRaw
  $script:dockerCache = @{ At = $now; Ready = $r }
  return $r
}

function Write-CommandLog {
  param([string]$Line)
  if ([string]::IsNullOrEmpty($Line)) { return }
  $ts = (Get-Date).ToString("HH:mm:ss")
  try {
    $window.Dispatcher.Invoke([action]{
      $ctrl.CommandLogBox.AppendText("[$ts] $Line`r`n")
      $ctrl.CommandLogBox.ScrollToEnd()
    })
  } catch {}
}

# ---- Dial control: arc gauge + sparkline ----------------------------------

function New-Dial {
  param(
    [string]$Key,
    [string]$Label,
    [string]$Unit,
    [double]$Min = 0,
    [double]$Max = 100
  )

  $root = New-Object System.Windows.Controls.Border
  $root.CornerRadius = New-Object System.Windows.CornerRadius 18
  $root.Background = (New-Brush 255 255 255)
  $root.BorderBrush = (New-Brush 217 229 243)
  $root.BorderThickness = New-Object System.Windows.Thickness 1
  $root.Padding = New-Object System.Windows.Thickness 10
  $root.Margin = New-Object System.Windows.Thickness 4
  $root.Cursor = [System.Windows.Input.Cursors]::Hand

  $stack = New-Object System.Windows.Controls.StackPanel
  $stack.HorizontalAlignment = "Center"

  $lbl = New-Object System.Windows.Controls.TextBlock
  $lbl.Text = $Label
  $lbl.FontSize = 11
  $lbl.FontWeight = "SemiBold"
  $lbl.Foreground = (New-Brush 100 116 139)
  $lbl.HorizontalAlignment = "Center"
  $lbl.Margin = New-Object System.Windows.Thickness 0,0,0,4
  [void]$stack.Children.Add($lbl)

  # Canvas holding the arc + center text. 110x110 inner.
  $canvas = New-Object System.Windows.Controls.Grid
  $canvas.Width = 110
  $canvas.Height = 110
  $canvas.HorizontalAlignment = "Center"

  $bg = New-Object System.Windows.Shapes.Path
  $bg.Stroke = (New-Brush 226 232 240)
  $bg.StrokeThickness = 11
  $bg.StrokeStartLineCap = "Round"
  $bg.StrokeEndLineCap = "Round"
  $bg.Data = New-DialGeometry -Pct 100 -Background
  [void]$canvas.Children.Add($bg)

  $fg = New-Object System.Windows.Shapes.Path
  $fg.Stroke = (New-Brush 47 116 219)
  $fg.StrokeThickness = 11
  $fg.StrokeStartLineCap = "Round"
  $fg.StrokeEndLineCap = "Round"
  $fg.Data = New-DialGeometry -Pct 0
  [void]$canvas.Children.Add($fg)

  $valStack = New-Object System.Windows.Controls.StackPanel
  $valStack.HorizontalAlignment = "Center"
  $valStack.VerticalAlignment = "Center"

  $valTxt = New-Object System.Windows.Controls.TextBlock
  $valTxt.Text = "-"
  $valTxt.FontSize = 22
  $valTxt.FontWeight = "Bold"
  $valTxt.HorizontalAlignment = "Center"
  $valTxt.Foreground = (New-Brush 15 23 42)
  [void]$valStack.Children.Add($valTxt)

  $unitTxt = New-Object System.Windows.Controls.TextBlock
  $unitTxt.Text = $Unit
  $unitTxt.FontSize = 10
  $unitTxt.Foreground = (New-Brush 100 116 139)
  $unitTxt.HorizontalAlignment = "Center"
  [void]$valStack.Children.Add($unitTxt)
  [void]$canvas.Children.Add($valStack)

  [void]$stack.Children.Add($canvas)

  # Sparkline
  $spark = New-Object System.Windows.Shapes.Polyline
  $spark.Stroke = (New-Brush 47 116 219)
  $spark.StrokeThickness = 1.5
  $spark.Width = 200
  $spark.Height = 22
  $spark.Margin = New-Object System.Windows.Thickness 0,6,0,0
  $spark.HorizontalAlignment = "Center"
  [void]$stack.Children.Add($spark)

  $root.Child = $stack
  $script:dialHistory[$Key] = New-Object 'System.Collections.Generic.Queue[double]'

  $script:dialCtrls[$Key] = @{
    Root  = $root
    Label = $lbl
    Value = $valTxt
    Unit  = $unitTxt
    Bg    = $bg
    Fg    = $fg
    Spark = $spark
    Min   = $Min
    Max   = $Max
  }
  return $root
}

function New-DialGeometry {
  param([double]$Pct, [switch]$Background)
  $cx = 55.0; $cy = 55.0; $r = 44.0
  # Sweep from 135deg to 405deg (270deg sweep), starting bottom-left
  $startDeg = 135.0
  $sweep = 270.0
  if ($Background) { $endDeg = $startDeg + $sweep } else {
    if ($Pct -lt 0) { $Pct = 0 }
    if ($Pct -gt 100) { $Pct = 100 }
    $endDeg = $startDeg + ($sweep * ($Pct / 100.0))
  }
  $sx = $cx + $r * [Math]::Cos($startDeg * [Math]::PI / 180.0)
  $sy = $cy + $r * [Math]::Sin($startDeg * [Math]::PI / 180.0)
  $ex = $cx + $r * [Math]::Cos($endDeg * [Math]::PI / 180.0)
  $ey = $cy + $r * [Math]::Sin($endDeg * [Math]::PI / 180.0)
  $largeArc = if (($endDeg - $startDeg) -gt 180) { 1 } else { 0 }
  if (-not $Background -and $Pct -le 0.01) {
    # Draw a degenerate point so path is valid
    return [System.Windows.Media.Geometry]::Parse(("M {0} {1}" -f $sx, $sy))
  }
  $d = "M {0} {1} A {2} {2} 0 {3} 1 {4} {5}" -f $sx, $sy, $r, $largeArc, $ex, $ey
  return [System.Windows.Media.Geometry]::Parse($d)
}

function Get-DialColor {
  param([double]$Pct)
  if ($Pct -ge 85) { return (New-Brush 220 38 38) }
  if ($Pct -ge 60) { return (New-Brush 217 119 6) }
  return (New-Brush 47 116 219)
}

function Update-Dial {
  param([string]$Key, $Value, [string]$DisplayText = $null)
  $d = $script:dialCtrls[$Key]
  if (-not $d) { return }
  if ($null -eq $Value) {
    $d.Value.Text = "-"
    $d.Fg.Data = New-DialGeometry -Pct 0
    return
  }
  $v = [double]$Value
  $pct = (($v - $d.Min) / [Math]::Max(0.001, ($d.Max - $d.Min))) * 100.0
  if ($pct -lt 0) { $pct = 0 }
  if ($pct -gt 100) { $pct = 100 }
  if ($DisplayText) { $d.Value.Text = $DisplayText } else { $d.Value.Text = "$([int]$v)" }
  $d.Fg.Data = New-DialGeometry -Pct $pct
  $d.Fg.Stroke = Get-DialColor -Pct $pct

  $hist = $script:dialHistory[$Key]
  $hist.Enqueue($pct)
  while ($hist.Count -gt 60) { [void]$hist.Dequeue() }
  $arr = @($hist.ToArray())
  if ($arr.Count -ge 2) {
    $w = $d.Spark.Width
    $h = $d.Spark.Height
    $pts = New-Object System.Windows.Media.PointCollection
    for ($i = 0; $i -lt $arr.Count; $i++) {
      $x = [double]$i / [double]($arr.Count - 1) * $w
      $y = $h - ([double]$arr[$i] / 100.0 * $h)
      $pts.Add((New-Object System.Windows.Point $x, $y))
    }
    $d.Spark.Points = $pts
    $d.Spark.Stroke = Get-DialColor -Pct $arr[-1]
  }
}

# ---- Status / metrics / requests ------------------------------------------

function Get-NetworkInfoSnapshot {
  param([switch]$Refresh)
  if ($Refresh -and (Test-Path $netInfoScript)) {
    try {
      & powershell -ExecutionPolicy Bypass -File $netInfoScript -Quiet | Out-Null
    } catch {
      Write-CommandLog "WARN: get_network_info.ps1 failed: $($_.Exception.Message)"
    }
  }
  if (-not (Test-Path $netInfoFile)) { return $null }
  try {
    return (Get-Content $netInfoFile -Raw -ErrorAction Stop | ConvertFrom-Json)
  } catch { return $null }
}

function Get-SystemMetricsSnapshot {
  param([switch]$IncludePerCore)
  # In-process call via the dot-sourced get_system_metrics.ps1 helpers.
  # Dropping the child powershell.exe shaves ~700-1500ms per call.
  try {
    if (Get-Command Get-SystemMetricsData -ErrorAction SilentlyContinue) {
      return Get-SystemMetricsData -IncludePerCore:$IncludePerCore
    }
  } catch {}
  return $null
}

function Set-Status {
  param([string]$BadgeKey, [string]$BadgeKind)
  $ctrl.TxtStatusBadge.Text = (T $BadgeKey)
  switch ($BadgeKind) {
    "ready" {
      $ctrl.StatusBadge.Background = (New-Brush 220 252 231)
      $ctrl.TxtStatusBadge.Foreground = (New-Brush 22 101 52)
      $ctrl.StatusDot.Fill = (New-Brush 5 150 105)
    }
    "busy" {
      $ctrl.StatusBadge.Background = (New-Brush 254 243 199)
      $ctrl.TxtStatusBadge.Foreground = (New-Brush 146 64 14)
      $ctrl.StatusDot.Fill = (New-Brush 217 119 6)
    }
    default {
      $ctrl.StatusBadge.Background = (New-Brush 226 232 240)
      $ctrl.TxtStatusBadge.Foreground = (New-Brush 51 65 85)
      $ctrl.StatusDot.Fill = (New-Brush 148 163 184)
    }
  }
}

function Set-HeroButton {
  param([string]$LabelKey, [string]$Variant, [bool]$Spinning, [bool]$Enabled)
  $btn = $ctrl.BtnPrimary
  $btn.Content = (T $LabelKey)
  $btn.IsEnabled = $Enabled
  $tpl = $btn.Template
  if ($tpl) {
    $border = $tpl.FindName("heroBd", $btn)
    $spinner = $tpl.FindName("HeroSpinner", $btn)
    if ($border) {
      $brush = New-Object System.Windows.Media.LinearGradientBrush
      $brush.StartPoint = New-Object System.Windows.Point 0, 0
      $brush.EndPoint = New-Object System.Windows.Point 1, 0
      $g1 = New-Object System.Windows.Media.GradientStop
      $g2 = New-Object System.Windows.Media.GradientStop
      switch ($Variant) {
        "stop" {
          $g1.Color = [System.Windows.Media.Color]::FromRgb(239,68,68); $g1.Offset = 0
          $g2.Color = [System.Windows.Media.Color]::FromRgb(185,28,28); $g2.Offset = 1
        }
        "busy" {
          $g1.Color = [System.Windows.Media.Color]::FromRgb(148,163,184); $g1.Offset = 0
          $g2.Color = [System.Windows.Media.Color]::FromRgb(100,116,139); $g2.Offset = 1
        }
        default {
          $g1.Color = [System.Windows.Media.Color]::FromRgb(75,132,228); $g1.Offset = 0
          $g2.Color = [System.Windows.Media.Color]::FromRgb(47,116,219); $g2.Offset = 1
        }
      }
      $brush.GradientStops.Add($g1) | Out-Null
      $brush.GradientStops.Add($g2) | Out-Null
      $border.Background = $brush
    }
    if ($spinner) {
      if ($Spinning) {
        $spinner.Visibility = "Visible"
        $rot = $spinner.RenderTransform
        if ($rot -is [System.Windows.Media.RotateTransform]) {
          $anim = New-Object System.Windows.Media.Animation.DoubleAnimation 0, 360, ([TimeSpan]::FromMilliseconds(1100))
          $anim.RepeatBehavior = [System.Windows.Media.Animation.RepeatBehavior]::Forever
          $rot.BeginAnimation([System.Windows.Media.RotateTransform]::AngleProperty, $anim)
        }
      } else {
        $spinner.Visibility = "Collapsed"
        $rot = $spinner.RenderTransform
        if ($rot -is [System.Windows.Media.RotateTransform]) {
          $rot.BeginAnimation([System.Windows.Media.RotateTransform]::AngleProperty, $null)
        }
      }
    }
  }
}

function Update-Status {
  param($Info = $null)

  $hs = $null
  if ($Info) { $hs = $Info.hotspot }
  $httpReady = $false
  $dnsReady = $false
  $hostReady = $false
  if ($hs -and $hs.validation) {
    $httpReady = [bool]$hs.validation.http_ready
    $dnsReady = [bool]$hs.validation.dns_ready
    $hostReady = [bool]$hs.validation.hostname_ready
  }
  $ready = ($hs -and $hs.status -eq "active" -and $httpReady)
  $script:lastReady = [bool]$ready

  if ($hostReady) {
    $script:connectUrl = "http://$hostname/"
  } elseif ($hs -and $hs.host_ip) {
    $script:connectUrl = "http://$($hs.host_ip)/"
  } elseif ($Info -and $Info.primary_url) {
    $script:connectUrl = $Info.primary_url
  } else {
    $script:connectUrl = "http://$hostname/"
  }

  $window.Dispatcher.Invoke([action]{
    try {
      if ($script:busy) {
        # Don't override busy display.
        $ctrl.TxtConnectUrl.Text = $script:connectUrl
        $ctrl.TxtSsidInline.Text = "SSID: $ssid"
        return
      }
      if ($script:lastReady) {
        $ctrl.TxtMainStatus.Text = (T "ready")
        $ctrl.TxtStatusHint.Text = (T "readyHint")
        Set-Status -BadgeKey "ready" -BadgeKind "ready"
        Set-HeroButton -LabelKey "stop" -Variant "stop" -Spinning $false -Enabled $true
      } else {
        $ctrl.TxtMainStatus.Text = (T "off")
        $ctrl.TxtStatusHint.Text = (T "idleHint")
        Set-Status -BadgeKey "off" -BadgeKind "off"
        Set-HeroButton -LabelKey "start" -Variant "start" -Spinning $false -Enabled $true
      }
      $ctrl.TxtConnectUrl.Text = $script:connectUrl
      $ctrl.TxtSsidInline.Text = "SSID: $ssid"
      if ($hs -and $hs.ssid) { $ctrl.TxtSsid.Text = [string]$hs.ssid } else { $ctrl.TxtSsid.Text = $ssid }
      if ($hs -and $hs.password) { $ctrl.TxtPassword.Text = [string]$hs.password } else { $ctrl.TxtPassword.Text = $key }
      if ($hs -and $hs.source) { $ctrl.TxtSource.Text = [string]$hs.source.interface_type_label } else { $ctrl.TxtSource.Text = (T "unknown") }
      $httpStr = if ($httpReady) { (T "yes") } else { (T "no") }
      $dnsStr = if ($dnsReady) { (T "yes") } else { (T "no") }
      $ctrl.TxtChecks.Text = "HTTP $httpStr / DNS $dnsStr"
    } catch {
      $st = $_.ScriptStackTrace; if (-not $st) { $st = "" }
      Write-BootstrapLog "Update-Status dispatcher action error: $($_.Exception.Message)`n  $st"
    }
  })
}

function Update-Metrics {
  param($Metrics = $null, $DockerReady = $null)
  # If not pre-fetched (legacy/manual callers), fall back to in-process collection.
  if ($null -eq $Metrics) { $Metrics = Get-SystemMetricsSnapshot }
  if ($null -eq $DockerReady) { $DockerReady = Test-DockerDaemonLocal }
  $metrics = $Metrics
  $dockerReady = [bool]$DockerReady
  $window.Dispatcher.Invoke([action]{
    try {
      if ($metrics) {
        $devices = 0
        try { $devices = [int]$metrics.hotspot.connected_device_count } catch {}
        $ctrl.TxtDevices.Text = [string]$devices

        $cpu = $null
        try { $cpu = $metrics.cpu.load_percent } catch {}
        if ($null -ne $cpu) { $script:lastCpuValue = [double]$cpu }
        $cpuText = if ($null -ne $cpu) { "$cpu%" } else { "-" }
        Update-Dial -Key "cpu" -Value $cpu -DisplayText $cpuText

        $gpuAvail = $false
        try { $gpuAvail = [bool]$metrics.gpu.available } catch {}
        if ($gpuAvail) {
          Update-Dial -Key "gpu" -Value $metrics.gpu.load_percent -DisplayText "$($metrics.gpu.load_percent)%"
          $temp = $metrics.gpu.temperature_c
          if ($null -ne $temp) {
            Update-Dial -Key "gputemp" -Value $temp -DisplayText "$([int]$temp)C"
          } else {
            Update-Dial -Key "gputemp" -Value $null
          }
          $clk = $metrics.gpu.graphics_clock_mhz
          if ($null -ne $clk) {
            Update-Dial -Key "gpuclk" -Value $clk -DisplayText "$([int]$clk)"
          } else {
            Update-Dial -Key "gpuclk" -Value $null
          }
        } else {
          Update-Dial -Key "gpu" -Value $null
          Update-Dial -Key "gputemp" -Value $null
          Update-Dial -Key "gpuclk" -Value $null
        }

        $rxBps = 0; $txBps = 0
        try { $rxBps = [double]$metrics.network.receive_bps } catch {}
        try { $txBps = [double]$metrics.network.send_bps } catch {}
        Update-Dial -Key "netDown" -Value $rxBps -DisplayText (Format-BytesPerSecond $rxBps)
        Update-Dial -Key "netUp"   -Value $txBps -DisplayText (Format-BytesPerSecond $txBps)
      } else {
        Update-Dial -Key "cpu" -Value $null
        Update-Dial -Key "gpu" -Value $null
        Update-Dial -Key "gputemp" -Value $null
        Update-Dial -Key "gpuclk" -Value $null
        Update-Dial -Key "netUp" -Value $null
        Update-Dial -Key "netDown" -Value $null
        $ctrl.TxtDevices.Text = "-"
      }
      if ($dockerReady) {
        $ctrl.DockerDot.Fill = (New-Brush 5 150 105)
        $ctrl.TxtDockerLine.Text = (T "dockerOn")
      } else {
        $ctrl.DockerDot.Fill = (New-Brush 148 163 184)
        $ctrl.TxtDockerLine.Text = (T "dockerOff")
      }
    } catch {
      $st = $_.ScriptStackTrace; if (-not $st) { $st = "" }
      Write-BootstrapLog "Update-Metrics dispatcher action error: $($_.Exception.Message)`n  $st"
    }
  })
}

# ---- Caddy access log -> request list ------------------------------------

function Get-RequestSummaryRows {
  if (-not (Test-Path $caddyAccessLog)) {
    return @(@{ Text = (T "requestMissing"); Severity = "info" })
  }
  $rows = @()
  try {
    foreach ($line in (Get-Content -LiteralPath $caddyAccessLog -Tail 80 -ErrorAction Stop)) {
      if ([string]::IsNullOrWhiteSpace($line)) { continue }
      try {
        $entry = $line | ConvertFrom-Json
        $ts = if ($entry.ts) { ([DateTimeOffset]::FromUnixTimeSeconds([int64]$entry.ts)).LocalDateTime.ToString("HH:mm:ss") } else { (Get-Date).ToString("HH:mm:ss") }
        $remote = "-"; $method = "-"; $uri = "-"
        if ($entry.request) {
          $remote = [string]$entry.request.remote_ip
          $method = [string]$entry.request.method
          $uri = [string]$entry.request.uri
        }
        if ([string]::IsNullOrWhiteSpace($uri)) { $uri = "/" }
        $pathOnly = ($uri -split "\?", 2)[0]
        $status = "-"
        if ($entry.status) { $status = [string]$entry.status }
        $sev = "info"
        $statusInt = 0
        try { $statusInt = [int]$status } catch {}
        if ($statusInt -ge 500) { $sev = "error" }
        elseif ($statusInt -ge 400) { $sev = "warn" }
        $rows += @{
          Text = ("[{0}] {1} {2} {3} -> {4}" -f $ts, $remote, $method, $pathOnly, $status)
          Severity = $sev
        }
      } catch {}
    }
  } catch {
    return @(@{ Text = (T "requestMissing"); Severity = "info" })
  }
  if ($rows.Count -eq 0) { return @(@{ Text = (T "noRequests"); Severity = "info" }) }
  return @($rows | Select-Object -Last 60)
}

function Update-RequestList {
  param($Rows = $null)
  if ($null -eq $Rows) { $Rows = Get-RequestSummaryRows }
  $rows = @($Rows)
  $fp = ($rows | ForEach-Object { $_.Text }) -join "`n"
  if ($fp -eq $script:lastRequestFingerprint) { return }
  $script:lastRequestFingerprint = $fp
  $window.Dispatcher.Invoke([action]{
    try {
      $ctrl.RequestList.Items.Clear()
      foreach ($r in $rows) {
        $item = New-Object System.Windows.Controls.ListBoxItem
        $item.Content = $r.Text
        switch ($r.Severity) {
          "error" { $item.Foreground = (New-Brush 252 165 165) }
          "warn"  { $item.Foreground = (New-Brush 253 224 71) }
          default { $item.Foreground = (New-Brush 203 213 225) }
        }
        [void]$ctrl.RequestList.Items.Add($item)
      }
      if ($ctrl.RequestList.Items.Count -gt 0) {
        $ctrl.RequestList.ScrollIntoView($ctrl.RequestList.Items[$ctrl.RequestList.Items.Count - 1])
      }
    } catch {
      $st = $_.ScriptStackTrace; if (-not $st) { $st = "" }
      Write-BootstrapLog "Update-RequestList dispatcher action error: $($_.Exception.Message)`n  $st"
    }
  })
}

# ---- Security events (direct sqlite read) ---------------------------------

function Test-Sqlite3Available {
  $cmd = Get-Command sqlite3.exe -ErrorAction SilentlyContinue
  return ($null -ne $cmd)
}

function Get-SecurityEvents {
  if (-not (Test-Path $securityDb)) { return @(@{ Text = (T "secMissing"); Severity = "info" }) }
  if (-not (Test-Sqlite3Available)) {
    return @(@{ Text = (T "secMissing") + " (sqlite3.exe)"; Severity = "info" })
  }
  $rows = @()
  try {
    $sql = "SELECT created_at, severity, event_type, COALESCE(username,'-'), COALESCE(ip,'-'), COALESCE(detail,'') FROM security_events ORDER BY created_ts DESC LIMIT 60;"
    # -readonly + -separator to keep parsing simple
    $output = & sqlite3.exe -readonly -separator "`t" $securityDb $sql 2>$null
    foreach ($line in $output) {
      if ([string]::IsNullOrWhiteSpace($line)) { continue }
      $parts = $line -split "`t", 6
      if ($parts.Count -lt 5) { continue }
      $when = $parts[0]; $sev = $parts[1]; $evt = $parts[2]; $user = $parts[3]; $ip = $parts[4]
      $det = if ($parts.Count -ge 6) { $parts[5] } else { "" }
      try {
        $dt = [DateTime]::Parse($when)
        $when = $dt.ToString("HH:mm:ss")
      } catch {}
      $sevKey = "info"
      switch -Wildcard ($sev.ToLower()) {
        "*crit*"  { $sevKey = "error" }
        "*high*"  { $sevKey = "error" }
        "*error*" { $sevKey = "error" }
        "*warn*"  { $sevKey = "warn" }
        default   { $sevKey = "info" }
      }
      $text = ("[{0}] {1,-5} {2} user={3} ip={4} {5}" -f $when, $sev, $evt, $user, $ip, $det)
      $rows += @{ Text = $text; Severity = $sevKey }
    }
  } catch {
    return @(@{ Text = "Security read error: $($_.Exception.Message)"; Severity = "warn" })
  }
  if ($rows.Count -eq 0) { return @(@{ Text = (T "secEmpty"); Severity = "info" }) }
  return $rows
}

function Update-SecurityList {
  param($Rows = $null)
  if ($null -eq $Rows) { $Rows = Get-SecurityEvents }
  $rows = @($Rows)
  $fp = ($rows | ForEach-Object { $_.Text }) -join "`n"
  if ($fp -eq $script:lastSecurityFingerprint) { return }
  $script:lastSecurityFingerprint = $fp
  $window.Dispatcher.Invoke([action]{
    try {
      $ctrl.SecurityList.Items.Clear()
      foreach ($r in $rows) {
        $item = New-Object System.Windows.Controls.ListBoxItem
        $item.Content = $r.Text
        switch ($r.Severity) {
          "error" { $item.Foreground = (New-Brush 252 165 165) }
          "warn"  { $item.Foreground = (New-Brush 253 224 71) }
          default { $item.Foreground = (New-Brush 203 213 225) }
        }
        $tag = $r.Text
        $item.Add_MouseDoubleClick({
          try { [System.Windows.Clipboard]::SetText($this.Content) } catch {}
        })
        [void]$ctrl.SecurityList.Items.Add($item)
      }
    } catch {
      $st = $_.ScriptStackTrace; if (-not $st) { $st = "" }
      Write-BootstrapLog "Update-SecurityList dispatcher action error: $($_.Exception.Message)`n  $st"
    }
  })
}

# ---- Per-core CPU popup ----------------------------------------------------

function Update-PerCorePopup {
  if (-not $script:perCoreVisible) { return }
  $metrics = Get-SystemMetricsSnapshot -IncludePerCore
  if (-not $metrics) { return }
  $cores = @()
  try { $cores = @($metrics.cpu.per_core) } catch {}
  $window.Dispatcher.Invoke([action]{
    try {
      $ctrl.CoreWrap.Children.Clear()
      foreach ($c in $cores) {
        $b = New-Object System.Windows.Controls.Border
        $b.CornerRadius = New-Object System.Windows.CornerRadius 12
        $b.Background = (New-Brush 248 251 255)
        $b.BorderBrush = (New-Brush 217 229 243)
        $b.BorderThickness = New-Object System.Windows.Thickness 1
        $b.Margin = New-Object System.Windows.Thickness 4
        $b.Padding = New-Object System.Windows.Thickness 10,8,10,8
        $b.Width = 88

        $sp = New-Object System.Windows.Controls.StackPanel
        $sp.HorizontalAlignment = "Center"

        $name = New-Object System.Windows.Controls.TextBlock
        $name.Text = "Core " + [string]$c.name
        $name.FontSize = 10
        $name.FontWeight = "SemiBold"
        $name.Foreground = (New-Brush 100 116 139)
        $name.HorizontalAlignment = "Center"
        [void]$sp.Children.Add($name)

        $val = New-Object System.Windows.Controls.TextBlock
        $val.Text = ([string]$c.load_percent + "%")
        $val.FontSize = 18
        $val.FontWeight = "Bold"
        $val.HorizontalAlignment = "Center"
        $pct = [double]$c.load_percent
        $val.Foreground = Get-DialColor -Pct $pct
        [void]$sp.Children.Add($val)

        $bar = New-Object System.Windows.Controls.ProgressBar
        $bar.Minimum = 0; $bar.Maximum = 100; $bar.Value = $pct
        $bar.Height = 6; $bar.Margin = New-Object System.Windows.Thickness 0,4,0,0
        $bar.Foreground = Get-DialColor -Pct $pct
        [void]$sp.Children.Add($bar)

        $b.Child = $sp
        [void]$ctrl.CoreWrap.Children.Add($b)
      }
    } catch {
      $st = $_.ScriptStackTrace; if (-not $st) { $st = "" }
      Write-BootstrapLog "Update-PerCorePopup dispatcher action error: $($_.Exception.Message)`n  $st"
    }
  })
}

# ---- Background script runner ---------------------------------------------

function Set-Busy {
  param([bool]$On, [string]$LabelKey = "starting")
  $script:busy = $On
  $window.Dispatcher.Invoke([action]{
    try {
      if ($On) {
        $ctrl.TxtMainStatus.Text = (T $LabelKey)
        $ctrl.TxtStatusHint.Text = if ($LabelKey -eq "stopping") { (T "stoppingHint") } else { (T "startingHint") }
        Set-Status -BadgeKey $LabelKey -BadgeKind "busy"
        Set-HeroButton -LabelKey $LabelKey -Variant "busy" -Spinning $true -Enabled $false
        $ctrl.StartProgress.Visibility = "Visible"
      } else {
        $ctrl.StartProgress.Visibility = "Collapsed"
      }
      $ctrl.BtnCopy.IsEnabled = -not $On
    } catch {
      $st = $_.ScriptStackTrace; if (-not $st) { $st = "" }
      Write-BootstrapLog "Set-Busy dispatcher action error: $($_.Exception.Message)`n  $st"
    }
  })
  if (-not $On) {
    # Force a fresh network probe + immediate status repaint after start/stop completes.
    if (Get-Command Refresh-Status -ErrorAction SilentlyContinue) {
      Refresh-Status -ForceNetRefresh
    } else {
      Update-Status
    }
  }
}

function Start-BackgroundScript {
  param([string]$Path, [string]$LabelKey)
  if ($script:busy) { return }
  if (-not (Test-Path $Path)) {
    Write-CommandLog "ERROR: script not found: $Path"
    return
  }
  Set-Busy -On $true -LabelKey $LabelKey
  Write-CommandLog "----- $(T $LabelKey) : $([System.IO.Path]::GetFileName($Path)) -----"

  $rs = [runspacefactory]::CreateRunspace()
  $rs.ApartmentState = "STA"
  $rs.Open()
  $rs.SessionStateProxy.SetVariable("uiPath", $Path)
  $rs.SessionStateProxy.SetVariable("uiWindow", $window)
  $rs.SessionStateProxy.SetVariable("uiLogBox", $ctrl.CommandLogBox)

  $ps = [powershell]::Create()
  $ps.Runspace = $rs
  [void]$ps.AddScript({
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = "powershell.exe"
    $psi.Arguments = '-ExecutionPolicy Bypass -File "' + $uiPath + '"'
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
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
    while (-not $proc.StandardError.EndOfStream) { & $append $proc.StandardError.ReadLine() $true }
    $proc.WaitForExit()
    return $proc.ExitCode
  })

  $handle = $ps.BeginInvoke()
  $script:activeJobs = @(@{ PS = $ps; Handle = $handle; RS = $rs; LabelKey = $LabelKey })

  $timer = New-Object System.Windows.Threading.DispatcherTimer
  $timer.Interval = [TimeSpan]::FromMilliseconds(300)
  $timer.Add_Tick({
    $job = $null
    try {
      if ($script:activeJobs.Count -gt 0) { $job = $script:activeJobs[0] }
    } catch {}
    if (-not $job -or -not $job.Handle) { return }
    if ($job.Handle.IsCompleted) {
      $timer.Stop()
      try {
        $exit = $job.PS.EndInvoke($job.Handle)
        Write-CommandLog ("----- {0} finished (exit={1}) -----" -f (T $job.LabelKey), $exit)
      } catch {
        Write-CommandLog ("----- {0} threw: {1} -----" -f (T $job.LabelKey), $_.Exception.Message)
      } finally {
        try { if ($job.PS) { $job.PS.Dispose() } } catch {}
        try { if ($job.RS) { $job.RS.Close() } } catch {}
        $script:activeJobs = @()
      }
      Set-Busy -On $false
      # Set-Busy already triggers Refresh-Status with ForceNetRefresh. Kick the rest
      # off too so the dials/listboxes catch up immediately, all off the UI thread.
      Refresh-Metrics
      Refresh-RequestList
      Refresh-SecurityList
    }
  })
  $timer.Start()
}

# ---- Locale apply ----------------------------------------------------------

function Set-LanguageLabels {
  $window.Title = (T "title")
  $ctrl.TxtTitle.Text = (T "title")
  $ctrl.TxtSubtitle.Text = (T "subtitle")
  $ctrl.BtnLang.Content = (T "lang")
  $ctrl.BtnCopy.Content = (T "copy")
  $ctrl.TxtHeroLabel.Text = (T "hero")
  $ctrl.TxtHotspotTitle.Text = (T "hotspot")
  $ctrl.TxtPerfTitle.Text = (T "perf")
  $ctrl.LblSsid.Text = (T "ssid")
  $ctrl.LblPassword.Text = (T "password")
  $ctrl.LblSource.Text = (T "source")
  $ctrl.LblChecks.Text = (T "checks")
  $ctrl.LblDevices.Text = (T "devices")
  $ctrl.TxtCommandTitle.Text = (T "commands")
  $ctrl.TxtCommandHint.Text = (T "cmdHint")
  $ctrl.TxtEventsTitle.Text = (T "events")
  $ctrl.TxtCoreTitle.Text = (T "coreTitle")
  $ctrl.TxtHotkeyHint.Text = (T "hotkey")
  if ($script:dialCtrls.cpu) { $script:dialCtrls.cpu.Label.Text = (T "cpu") }
  if ($script:dialCtrls.gpu) { $script:dialCtrls.gpu.Label.Text = (T "gpu") }
  if ($script:dialCtrls.gputemp) { $script:dialCtrls.gputemp.Label.Text = (T "gputemp") }
  if ($script:dialCtrls.gpuclk) { $script:dialCtrls.gpuclk.Label.Text = (T "gpuclk") }
  if ($script:dialCtrls.netDown) { $script:dialCtrls.netDown.Label.Text = (T "netDown") }
  if ($script:dialCtrls.netUp) { $script:dialCtrls.netUp.Label.Text = (T "netUp") }
}

# ---- Build dial grid -------------------------------------------------------

[void]$ctrl.DialGrid.Children.Add((New-Dial -Key "cpu" -Label "CPU" -Unit "%" -Min 0 -Max 100))
[void]$ctrl.DialGrid.Children.Add((New-Dial -Key "gpu" -Label "GPU" -Unit "%" -Min 0 -Max 100))
[void]$ctrl.DialGrid.Children.Add((New-Dial -Key "gputemp" -Label "GPU Temp" -Unit "C" -Min 30 -Max 95))
[void]$ctrl.DialGrid.Children.Add((New-Dial -Key "gpuclk" -Label "GPU Clock" -Unit "MHz" -Min 200 -Max 2200))
[void]$ctrl.DialGrid.Children.Add((New-Dial -Key "netDown" -Label "Down" -Unit "B/s" -Min 0 -Max 12500000))
[void]$ctrl.DialGrid.Children.Add((New-Dial -Key "netUp" -Label "Up" -Unit "B/s" -Min 0 -Max 12500000))

# CPU dial click -> popup with per-core breakdown
$script:dialCtrls.cpu.Root.Add_MouseLeftButtonUp({
  $script:perCoreVisible = -not $script:perCoreVisible
  $ctrl.CorePopup.IsOpen = $script:perCoreVisible
  if ($script:perCoreVisible) { Update-PerCorePopup }
})
$ctrl.CorePopup.Add_Closed({ $script:perCoreVisible = $false })

Write-BootstrapLog "Dial grid built"

# ---- Status dot pulse ------------------------------------------------------

$pulseAnim = New-Object System.Windows.Media.Animation.DoubleAnimation 1.0, 0.4, ([TimeSpan]::FromSeconds(1.6))
$pulseAnim.AutoReverse = $true
$pulseAnim.RepeatBehavior = [System.Windows.Media.Animation.RepeatBehavior]::Forever
$ctrl.StatusDot.BeginAnimation([System.Windows.UIElement]::OpacityProperty, $pulseAnim)

# ---- Event handlers --------------------------------------------------------

$ctrl.BtnPrimary.Add_Click({
  if ($script:lastReady) {
    Start-BackgroundScript -Path $downScript -LabelKey "stopping"
  } else {
    $ctrl.CommandLogBox.Clear()
    Start-BackgroundScript -Path $upScript -LabelKey "starting"
  }
})

$ctrl.BtnCopy.Add_Click({
  try {
    [System.Windows.Clipboard]::SetText($script:connectUrl)
    $ctrl.TxtFooter.Text = "$(T 'copied'): $script:connectUrl"
  } catch {
    $ctrl.TxtFooter.Text = "Copy failed: $($_.Exception.Message)"
  }
})

$ctrl.BtnLang.Add_Click({
  if ($script:language -eq "es") { $script:language = "en" } else { $script:language = "es" }
  Set-LanguageLabels
  # Trigger background refreshes so any locale-dependent placeholder text updates without
  # blocking the UI thread. The labels above already give immediate visual feedback.
  Refresh-Status
  Refresh-Metrics
  Refresh-RequestList
  Refresh-SecurityList
  Save-Prefs @{
    language = $script:language
    windowWidth = $window.Width
    windowHeight = $window.Height
  }
})

# Hotkeys
$window.Add_KeyDown({
  param($s, $e)
  if ($e.KeyboardDevice.Modifiers -band [System.Windows.Input.ModifierKeys]::Control) {
    switch ($e.Key) {
      "L" { $ctrl.CommandLogBox.Clear(); $e.Handled = $true }
      "E" { [void]$ctrl.SecurityList.Focus(); $e.Handled = $true }
      "Q" { $window.Close(); $e.Handled = $true }
    }
  }
})

$window.Add_Closing({
  Save-Prefs @{
    language = $script:language
    windowWidth = $window.Width
    windowHeight = $window.Height
  }
})

# ---- Background refresh wrappers ------------------------------------------
# Each wrapper submits the slow data fetch to the background runspace. The OnResult
# callback runs on the UI thread (DispatcherTimer.Tick fires there), so it can safely
# touch $ctrl.* by calling the existing Update-* functions.

# get_network_info.ps1 is heavy (TCP probes with 1.2s timeouts). Throttle the refresh
# to once every 30s during normal polling. start/stop transitions still force-refresh
# via Set-Busy.
$script:lastNetRefresh = [DateTime]::MinValue

function Refresh-Status {
  param([switch]$ForceNetRefresh)
  $shouldRefresh = $ForceNetRefresh.IsPresent -or `
    ((Get-Date) - $script:lastNetRefresh).TotalSeconds -ge 30
  if ($shouldRefresh) { $script:lastNetRefresh = Get-Date }

  Invoke-Background -Key "status" `
    -Arguments @([bool]$shouldRefresh, [string]$netInfoScript, [string]$netInfoFile) `
    -Work {
      param($refresh, $script, $file)
      return bgGet-NetworkInfo -Refresh ([bool]$refresh) -Script $script -File $file
    } `
    -OnResult {
      param($info)
      try { Update-Status -Info $info }
      catch { Write-BootstrapLog "Update-Status callback error: $($_.Exception.Message)" }
    }
}

function Refresh-Metrics {
  Invoke-Background -Key "metrics" `
    -Work {
      # $metricsScriptPath is injected via SessionStateProxy in Invoke-Background.
      $m = $null
      try {
        if ((Test-Path $metricsScriptPath) -and -not (Get-Command Get-SystemMetricsData -ErrorAction SilentlyContinue)) {
          . $metricsScriptPath
        }
        $m = Get-SystemMetricsData
      } catch {}
      $d = bgGet-DockerReady
      return [pscustomobject]@{ Metrics = $m; Docker = $d }
    } `
    -OnResult {
      param($r)
      try {
        if ($r) {
          # Mirror the result into the UI-thread docker cache so other callers see
          # the same value without having to re-shell to docker info.
          $script:dockerCache = @{ At = (Get-Date); Ready = [bool]$r.Docker }
          Update-Metrics -Metrics $r.Metrics -DockerReady ([bool]$r.Docker)
        }
        if ($script:perCoreVisible) { Update-PerCorePopup }
      } catch {
        Write-BootstrapLog "Update-Metrics callback error: $($_.Exception.Message)"
      }
    }
}

function Refresh-RequestList {
  Invoke-Background -Key "requests" `
    -Arguments @([string]$caddyAccessLog, [string](T "requestMissing"), [string](T "noRequests")) `
    -Work {
      param($logPath, $missing, $empty)
      return bgGet-RequestRows -LogPath $logPath -RequestMissingText $missing -NoRequestsText $empty
    } `
    -OnResult {
      param($rows)
      try { Update-RequestList -Rows $rows }
      catch { Write-BootstrapLog "Update-RequestList callback error: $($_.Exception.Message)" }
    }
}

function Refresh-SecurityList {
  Invoke-Background -Key "security" `
    -Arguments @([string]$securityDb, [string](T "secMissing"), [string](T "secEmpty")) `
    -Work {
      param($db, $missing, $empty)
      return bgGet-SecurityRows -DbPath $db -MissingText $missing -EmptyText $empty
    } `
    -OnResult {
      param($rows)
      try { Update-SecurityList -Rows $rows }
      catch { Write-BootstrapLog "Update-SecurityList callback error: $($_.Exception.Message)" }
    }
}

# Defensive: any uncaught exception in a WPF event handler (DispatcherTimer.Tick, button
# click, etc.) would otherwise propagate out of ShowDialog and kill the window. Log it
# in detail and mark Handled so the message pump keeps running.
Register-DispatcherUnhandledExceptionHandler -Dispatcher $window.Dispatcher

# ---- Timers ----------------------------------------------------------------

$statusTimer = New-Object System.Windows.Threading.DispatcherTimer
$statusTimer.Interval = [TimeSpan]::FromSeconds(5)
$statusTimer.Add_Tick({
  try { Refresh-Status }
  catch { Write-BootstrapLog "statusTimer tick error: $($_.Exception.Message)" }
})
$statusTimer.Start()

$metricsTimer = New-Object System.Windows.Threading.DispatcherTimer
$metricsTimer.Interval = [TimeSpan]::FromSeconds(3)
$metricsTimer.Add_Tick({
  try { Refresh-Metrics }
  catch { Write-BootstrapLog "metricsTimer tick error: $($_.Exception.Message)" }
})
$metricsTimer.Start()

$requestTimer = New-Object System.Windows.Threading.DispatcherTimer
$requestTimer.Interval = [TimeSpan]::FromSeconds(2)
$requestTimer.Add_Tick({
  try { Refresh-RequestList }
  catch { Write-BootstrapLog "requestTimer tick error: $($_.Exception.Message)" }
})
$requestTimer.Start()

$securityTimer = New-Object System.Windows.Threading.DispatcherTimer
$securityTimer.Interval = [TimeSpan]::FromSeconds(5)
$securityTimer.Add_Tick({
  try { Refresh-SecurityList }
  catch { Write-BootstrapLog "securityTimer tick error: $($_.Exception.Message)" }
})
$securityTimer.Start()

# ---- First paint -----------------------------------------------------------

Set-LanguageLabels
$ctrl.TxtSsid.Text = $ssid
$ctrl.TxtPassword.Text = $key
$ctrl.TxtFooter.Text = "Admin: $env:COMPUTERNAME"

# Defer first refresh to after window is shown so user sees UI immediately.
$window.Add_ContentRendered({
  if ($script:firstShowDone) { return }
  $script:firstShowDone = $true
  Write-BootstrapLog "Window content rendered"
  $window.Topmost = $true
  $window.Activate()
  $window.Topmost = $false
  try {
    # Step 1: paint sensible defaults synchronously so the window doesn't appear half-empty
    # while the slow get_network_info.ps1 refresh (1.5-3s) finishes in the background.
    # Update-* with null data uses the already-defined fallbacks (Off/Start system/-).
    try { Update-Status -Info $null } catch { Write-BootstrapLog "Default Update-Status failed: $($_.Exception.Message)" }
    try { Update-Metrics -Metrics $null -DockerReady $false } catch { Write-BootstrapLog "Default Update-Metrics failed: $($_.Exception.Message)" }
    try { Update-RequestList -Rows @(@{ Text = (T "requestMissing"); Severity = "info" }) } catch { Write-BootstrapLog "Default Update-RequestList failed: $($_.Exception.Message)" }
    try { Update-SecurityList -Rows @(@{ Text = (T "secMissing"); Severity = "info" }) } catch { Write-BootstrapLog "Default Update-SecurityList failed: $($_.Exception.Message)" }

    # Step 2: schedule the real refreshes (off the UI thread).
    Refresh-Status
    Refresh-Metrics
    Refresh-RequestList
    Refresh-SecurityList
  } catch {
    Write-BootstrapLog "First refresh failed: $($_.Exception.Message)"
  }
  Write-CommandLog "Puente Admin listo. SSID: $ssid / Hostname: $hostname"
})

Write-BootstrapLog "Calling ShowDialog"
try {
  [void]$window.ShowDialog()
  Write-BootstrapLog "ShowDialog returned (window closed normally)"
} catch {
  $err = $_
  $stack = $err.ScriptStackTrace
  if (-not $stack) { $stack = "(no script stack)" }
  $pos = ""
  try { $pos = $err.InvocationInfo.PositionMessage } catch {}
  if (-not $pos) { $pos = "(no position)" }
  $inner = ""
  $ex = $err.Exception
  while ($ex -and $ex.InnerException) {
    $ex = $ex.InnerException
    $inner += "`n  inner: " + $ex.GetType().FullName + ": " + $ex.Message
    if ($ex.StackTrace) {
      $inner += "`n    " + ($ex.StackTrace -replace "`r?`n", "`n    ")
    }
  }
  $clrStack = ""
  try {
    if ($err.Exception.StackTrace) {
      $clrStack = "`nCLRStack:`n  " + ($err.Exception.StackTrace -replace "`r?`n", "`n  ")
    }
  } catch {}
  Show-FatalBootstrapError ("ShowDialog crashed: " + $err.Exception.Message + "`nPosition: " + $pos + "`nScriptStack:`n  " + ($stack -replace "`r?`n", "`n  ") + $clrStack + $inner)
}
