# Puente host admin console. Local elevated WPF panel for the host computer only.
#
# Sections:
#   1. Bootstrap, paths, elevation, fatal handler
#   2. WPF + XAML (logo + lang toggle + Start/Stop hero + Wi-Fi card)
#   3. State, helpers, button machine, Start-BackgroundScript
#   4. Event handlers, status timer, first paint + ShowDialog

param(
  [switch]$NoElevate,
  [switch]$SelfTest,
  [switch]$AdminRequiredSelfTest
)

$ErrorActionPreference = "Stop"

# Earliest possible diagnostic — confirms the script *entered* (vs. powershell.exe
# bailing during arg parsing). Derive the log dir from the script's own location
# so the repo is fully relocatable.
try {
  $_earlyScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
  $_earlyToolsDir  = Split-Path -Parent (Split-Path -Parent $_earlyScriptDir)
  $_earlyAiboxDir  = Split-Path -Parent $_earlyToolsDir
  $_earlyLogDir    = Join-Path $_earlyAiboxDir "backend-data\appdata\host-admin"
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
$logoPath        = Join-Path $stackDir "portal\assets\circlelogo.png"
$bootstrapLogDir = Join-Path $aiboxDir "backend-data\appdata\host-admin"
$bootstrapLogFile= Join-Path $bootstrapLogDir "ui-bootstrap.log"
$prefsFile       = Join-Path $bootstrapLogDir "ui-prefs.json"

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

Write-BootstrapLog "Launching simplified admin console. IsAdmin=$(Test-IsAdministrator) SelfTest=$SelfTest"

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
} catch {
  Show-FatalBootstrapError "No se pudieron cargar las librerias WPF. $($_.Exception.Message)"
}
Write-BootstrapLog "WPF assemblies loaded"

# ---- XAML ------------------------------------------------------------------

[xml]$xaml = @"
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Consola Puente Admin" Height="480" Width="720" MinHeight="420" MinWidth="640"
        WindowStartupLocation="CenterScreen" FontFamily="Aptos, Segoe UI, Tahoma"
        Foreground="#0F172A" Background="#F5F8FC">
  <Window.Resources>
    <Style x:Key="GhostBtn" TargetType="Button">
      <Setter Property="MinHeight" Value="40"/>
      <Setter Property="Padding" Value="16,0"/>
      <Setter Property="Background" Value="#FFFFFF"/>
      <Setter Property="Foreground" Value="#1B4F9C"/>
      <Setter Property="BorderBrush" Value="#D9E5F3"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="Cursor" Value="Hand"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="Button">
            <Border x:Name="bd" CornerRadius="12"
                    Background="{TemplateBinding Background}"
                    BorderBrush="{TemplateBinding BorderBrush}"
                    BorderThickness="{TemplateBinding BorderThickness}">
              <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"
                                Margin="{TemplateBinding Padding}"/>
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

    <Style x:Key="Card" TargetType="Border">
      <Setter Property="CornerRadius" Value="18"/>
      <Setter Property="Background" Value="#FFFFFF"/>
      <Setter Property="BorderBrush" Value="#D9E5F3"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="Padding" Value="24"/>
      <Setter Property="Effect">
        <Setter.Value>
          <DropShadowEffect BlurRadius="22" ShadowDepth="0" Color="#1B4F8A" Opacity="0.10"/>
        </Setter.Value>
      </Setter>
    </Style>

    <Style x:Key="WifiLabel" TargetType="TextBlock">
      <Setter Property="FontSize" Value="13"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="Foreground" Value="#64748B"/>
      <Setter Property="VerticalAlignment" Value="Center"/>
      <Setter Property="Margin" Value="0,0,0,14"/>
    </Style>
    <Style x:Key="WifiValue" TargetType="TextBlock">
      <Setter Property="FontSize" Value="22"/>
      <Setter Property="FontWeight" Value="Bold"/>
      <Setter Property="Foreground" Value="#0F172A"/>
      <Setter Property="FontFamily" Value="Cascadia Code, Consolas"/>
      <Setter Property="VerticalAlignment" Value="Center"/>
      <Setter Property="Margin" Value="0,0,0,14"/>
      <Setter Property="TextTrimming" Value="CharacterEllipsis"/>
    </Style>
  </Window.Resources>

  <Grid Margin="22">
    <Grid.RowDefinitions>
      <RowDefinition Height="Auto"/>
      <RowDefinition Height="*"/>
      <RowDefinition Height="Auto"/>
    </Grid.RowDefinitions>

    <!-- ROW 0: top bar -->
    <Grid Grid.Row="0" Margin="0,0,0,20">
      <Grid.ColumnDefinitions>
        <ColumnDefinition Width="*"/>
        <ColumnDefinition Width="Auto"/>
      </Grid.ColumnDefinitions>

      <!-- Left: logo + titles -->
      <StackPanel Orientation="Horizontal" VerticalAlignment="Center">
        <Border Width="56" Height="56" CornerRadius="28" Background="#EDF4FF" Margin="0,0,16,0">
          <Image Name="LogoImage" Stretch="UniformToFill"/>
        </Border>
        <StackPanel VerticalAlignment="Center">
          <TextBlock Name="TxtTitle" FontSize="22" FontWeight="Bold" Foreground="#0F172A"/>
          <TextBlock Name="TxtSubtitle" FontSize="12" Foreground="#475569" Margin="0,2,0,0"/>
        </StackPanel>
      </StackPanel>

      <!-- Right: stacked lang toggle + hero button -->
      <StackPanel Grid.Column="1" Orientation="Vertical" HorizontalAlignment="Right">
        <Button Name="BtnLang" Style="{StaticResource GhostBtn}"
                Width="120" HorizontalAlignment="Right" Margin="0,0,0,12"/>

        <Button Name="BtnPrimary" Width="220" Height="56"
                FontSize="17" FontWeight="Bold" Foreground="White"
                BorderThickness="0" Cursor="Hand">
          <Button.Template>
            <ControlTemplate TargetType="Button">
              <Border x:Name="heroBd" CornerRadius="18">
                <Border.Background>
                  <LinearGradientBrush StartPoint="0,0" EndPoint="1,0">
                    <GradientStop Color="#4B84E4" Offset="0"/>
                    <GradientStop Color="#2F74DB" Offset="1"/>
                  </LinearGradientBrush>
                </Border.Background>
                <Border.Effect>
                  <DropShadowEffect BlurRadius="18" ShadowDepth="0" Color="#1B4F9C" Opacity="0.40"/>
                </Border.Effect>
                <Grid>
                  <Path Name="HeroSpinner" Stroke="#FFFFFF" StrokeThickness="3"
                        Width="22" Height="22" HorizontalAlignment="Left" Margin="20,0,0,0"
                        VerticalAlignment="Center" Visibility="Collapsed"
                        Data="M 11 1.5 A 9.5 9.5 0 1 1 4.3 4.3">
                    <Path.RenderTransform>
                      <RotateTransform x:Name="SpinnerRot" Angle="0" CenterX="11" CenterY="11"/>
                    </Path.RenderTransform>
                  </Path>
                  <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
                </Grid>
              </Border>
              <ControlTemplate.Triggers>
                <Trigger Property="IsEnabled" Value="False">
                  <Setter Property="Opacity" Value="0.9"/>
                </Trigger>
                <Trigger Property="IsMouseOver" Value="True">
                  <Setter TargetName="heroBd" Property="Effect">
                    <Setter.Value>
                      <DropShadowEffect BlurRadius="26" ShadowDepth="0" Color="#1B4F9C" Opacity="0.60"/>
                    </Setter.Value>
                  </Setter>
                </Trigger>
              </ControlTemplate.Triggers>
            </ControlTemplate>
          </Button.Template>
        </Button>
      </StackPanel>
    </Grid>

    <!-- ROW 1: Wi-Fi info card -->
    <Border Grid.Row="1" Style="{StaticResource Card}">
      <Grid>
        <Grid.ColumnDefinitions>
          <ColumnDefinition Width="160"/>
          <ColumnDefinition Width="*"/>
        </Grid.ColumnDefinitions>
        <Grid.RowDefinitions>
          <RowDefinition Height="Auto"/>
          <RowDefinition Height="Auto"/>
          <RowDefinition Height="Auto"/>
        </Grid.RowDefinitions>

        <TextBlock Name="LblSsid"     Grid.Row="0" Grid.Column="0" Style="{StaticResource WifiLabel}"/>
        <TextBlock Name="TxtSsid"     Grid.Row="0" Grid.Column="1" Style="{StaticResource WifiValue}"/>

        <TextBlock Name="LblPassword" Grid.Row="1" Grid.Column="0" Style="{StaticResource WifiLabel}"/>
        <TextBlock Name="TxtPassword" Grid.Row="1" Grid.Column="1" Style="{StaticResource WifiValue}"/>

        <TextBlock Name="LblIPv4"     Grid.Row="2" Grid.Column="0" Style="{StaticResource WifiLabel}"/>
        <TextBlock Name="TxtIPv4"     Grid.Row="2" Grid.Column="1" Style="{StaticResource WifiValue}" Foreground="#1B4F9C"/>
      </Grid>
    </Border>

    <!-- ROW 2: footer status -->
    <TextBlock Grid.Row="2" Name="TxtFooter" FontSize="11" Foreground="#94A3B8" Margin="4,12,0,0"/>
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
  "LogoImage","TxtTitle","TxtSubtitle",
  "BtnLang","BtnPrimary",
  "LblSsid","TxtSsid","LblPassword","TxtPassword","LblIPv4","TxtIPv4",
  "TxtFooter"
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
} else {
  Write-BootstrapLog "Logo file missing at $logoPath; circle will be blank."
}

# ---- State + locale --------------------------------------------------------

$prefs = Read-Prefs
$script:language       = if ($prefs.ContainsKey("language") -and $prefs.language) { [string]$prefs.language } else { "es" }
$script:state          = "Off"           # Off | Starting | Ready | Stopping
$script:lastIp         = $null
$script:lastIpMode     = "none"
$script:firstShowDone  = $false
$script:activeJob      = $null
$script:activeTimer    = $null

if ($prefs.ContainsKey("windowWidth") -and $prefs.windowWidth) {
  try { $window.Width = [double]$prefs.windowWidth } catch {}
}
if ($prefs.ContainsKey("windowHeight") -and $prefs.windowHeight) {
  try { $window.Height = [double]$prefs.windowHeight } catch {}
}

# ---- Translation strings ---------------------------------------------------

$Text = @{
  es = @{
    title             = "Consola Puente Admin"
    subtitle          = "Panel de control"
    lang              = "English"
    start             = "Iniciar"
    starting          = "Iniciando..."
    stop              = "Detener"
    stopping          = "Deteniendo..."
    ssid              = "Red Wi-Fi"
    password          = "Contrasena"
    ipv4              = "Direccion IP"
    ipUnavailable     = "Inicia el sistema"
    footerReady       = "Listo - {0}"
    footerOff         = "Sistema apagado"
    footerStarting    = "Iniciando el sistema..."
    footerStopping    = "Deteniendo el sistema..."
    footerError       = "Error: {0}"
    confirmCloseTitle = "Cerrar Consola Puente"
    confirmCloseBody  = "El sistema esta en proceso. Si cierras la ventana, los comandos continuaran en segundo plano.`n`nCerrar de todos modos?"
  }
  en = @{
    title             = "Puente Admin Console"
    subtitle          = "Control panel"
    lang              = "Espanol"
    start             = "Start"
    starting          = "Starting..."
    stop              = "Stop"
    stopping          = "Stopping..."
    ssid              = "Wi-Fi network"
    password          = "Password"
    ipv4              = "IP address"
    ipUnavailable     = "Start the system"
    footerReady       = "Ready - {0}"
    footerOff         = "System off"
    footerStarting    = "Starting the system..."
    footerStopping    = "Stopping the system..."
    footerError       = "Error: {0}"
    confirmCloseTitle = "Close Puente Console"
    confirmCloseBody  = "The system is in progress. If you close the window, the commands will keep running in the background.`n`nClose anyway?"
  }
}

if (-not $Text.ContainsKey($script:language)) {
  Write-BootstrapLog "Unsupported UI language '$script:language' in prefs; defaulting to es."
  $script:language = "es"
}

function T {
  param([string]$Key)
  $lang = $script:language
  if (-not $Text.ContainsKey($lang)) { $lang = "es" }
  if ($Text[$lang].ContainsKey($Key)) { return $Text[$lang][$Key] }
  if ($Text.en.ContainsKey($Key)) { return $Text.en[$Key] }
  return $Key
}

function TF {
  param(
    [string]$Key,
    [Parameter(ValueFromRemainingArguments = $true)] [object[]]$FormatArgs
  )
  $template = [string](T $Key)
  return [regex]::Replace($template, '\{(\d+)\}', {
    param($m)
    $idx = [int]$m.Groups[1].Value
    if ($idx -ge 0 -and $idx -lt $FormatArgs.Count) {
      return [string]$FormatArgs[$idx]
    }
    return $m.Value
  })
}

# ---- Locale apply ----------------------------------------------------------

function Set-LanguageLabels {
  $window.Title          = (T "title")
  $ctrl.TxtTitle.Text    = (T "title")
  $ctrl.TxtSubtitle.Text = (T "subtitle")
  $ctrl.BtnLang.Content  = (T "lang")
  $ctrl.LblSsid.Text     = (T "ssid")
  $ctrl.LblPassword.Text = (T "password")
  $ctrl.LblIPv4.Text     = (T "ipv4")
  # Re-apply the current state's button label so a mid-flight language toggle picks up immediately.
  Set-ButtonState -State $script:state -SkipBrush
  # Refresh the Wi-Fi panel so the IPv4 placeholder text matches the new language.
  Update-WifiPanel
  # Refresh footer too.
  Update-Footer
}

# ---- Host IPv4 detection ---------------------------------------------------

function Get-HostIPv4 {
  # Returns hashtable: @{ Mode = "hotspot"|"lan"|"none"; IP = "192.168.137.1"|...|$null }
  # Priority: live hotspot adapter -> network-info.json -> first non-virtual LAN IPv4.
  try {
    $hotspot = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
      Where-Object {
        $_.IPAddress -like "192.168.137.*" -and
        (Get-NetIPInterface -InterfaceIndex $_.InterfaceIndex -AddressFamily IPv4 `
          -ErrorAction SilentlyContinue).ConnectionState -eq 'Connected'
      } |
      Select-Object -First 1
    if ($hotspot) { return @{ Mode = "hotspot"; IP = [string]$hotspot.IPAddress } }
  } catch {}

  if (Test-Path $netInfoFile) {
    try {
      $ni = Get-Content $netInfoFile -Raw -ErrorAction Stop | ConvertFrom-Json
      if ($ni.hotspot -and $ni.hotspot.host_ip) { return @{ Mode = "hotspot"; IP = [string]$ni.hotspot.host_ip } }
      if ($ni.lan -and $ni.lan.primary_ip)      { return @{ Mode = "lan";     IP = [string]$ni.lan.primary_ip  } }
    } catch {}
  }

  try {
    $cand = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
      Where-Object {
        $_.IPAddress -notlike "127.*"        -and
        $_.IPAddress -notlike "169.254.*"    -and
        $_.IPAddress -notlike "172.1[6-9].*" -and
        $_.IPAddress -notlike "172.2[0-9].*" -and
        $_.IPAddress -notlike "172.3[0-1].*" -and
        $_.PrefixOrigin -ne 'WellKnown' -and
        (Get-NetIPInterface -InterfaceIndex $_.InterfaceIndex -AddressFamily IPv4 `
          -ErrorAction SilentlyContinue).ConnectionState -eq 'Connected'
      } |
      Select-Object -First 1
    if ($cand) { return @{ Mode = "lan"; IP = [string]$cand.IPAddress } }
  } catch {}

  return @{ Mode = "none"; IP = $null }
}

# ---- Hero button brush + spinner -------------------------------------------

function New-HeroBrush {
  param([string]$Variant)   # "start" | "stop" | "busy"
  $brush = New-Object System.Windows.Media.LinearGradientBrush
  $brush.StartPoint = New-Object System.Windows.Point 0, 0
  $brush.EndPoint   = New-Object System.Windows.Point 1, 0
  $g1 = New-Object System.Windows.Media.GradientStop
  $g2 = New-Object System.Windows.Media.GradientStop
  switch ($Variant) {
    "stop" {
      $g1.Color = [System.Windows.Media.Color]::FromRgb(239, 68, 68);  $g1.Offset = 0
      $g2.Color = [System.Windows.Media.Color]::FromRgb(185, 28, 28);  $g2.Offset = 1
    }
    "busy" {
      $g1.Color = [System.Windows.Media.Color]::FromRgb(148, 163, 184); $g1.Offset = 0
      $g2.Color = [System.Windows.Media.Color]::FromRgb(100, 116, 139); $g2.Offset = 1
    }
    default {
      $g1.Color = [System.Windows.Media.Color]::FromRgb( 75, 132, 228); $g1.Offset = 0
      $g2.Color = [System.Windows.Media.Color]::FromRgb( 47, 116, 219); $g2.Offset = 1
    }
  }
  [void]$brush.GradientStops.Add($g1)
  [void]$brush.GradientStops.Add($g2)
  return $brush
}

function Start-SpinnerAnim {
  param($Spinner)
  if (-not $Spinner) { return }
  $Spinner.Visibility = "Visible"
  $rot = $Spinner.RenderTransform
  if ($rot -is [System.Windows.Media.RotateTransform]) {
    $anim = New-Object System.Windows.Media.Animation.DoubleAnimation 0, 360, ([TimeSpan]::FromMilliseconds(1100))
    $anim.RepeatBehavior = [System.Windows.Media.Animation.RepeatBehavior]::Forever
    $rot.BeginAnimation([System.Windows.Media.RotateTransform]::AngleProperty, $anim)
  }
}

function Stop-SpinnerAnim {
  param($Spinner)
  if (-not $Spinner) { return }
  $Spinner.Visibility = "Collapsed"
  $rot = $Spinner.RenderTransform
  if ($rot -is [System.Windows.Media.RotateTransform]) {
    $rot.BeginAnimation([System.Windows.Media.RotateTransform]::AngleProperty, $null)
  }
}

# ---- Button state machine --------------------------------------------------

function Set-ButtonState {
  param(
    [ValidateSet("Off","Starting","Ready","Stopping")]
    [string]$State,
    [switch]$SkipBrush
  )
  $script:state = $State
  $window.Dispatcher.Invoke([action]{
    try {
      $btn = $ctrl.BtnPrimary
      switch ($State) {
        "Off"      { $labelKey = "start";    $variant = "start"; $enabled = $true;  $spin = $false }
        "Starting" { $labelKey = "starting"; $variant = "busy";  $enabled = $false; $spin = $true  }
        "Ready"    { $labelKey = "stop";     $variant = "stop";  $enabled = $true;  $spin = $false }
        "Stopping" { $labelKey = "stopping"; $variant = "busy";  $enabled = $false; $spin = $true  }
      }
      $btn.Content   = (T $labelKey)
      $btn.IsEnabled = $enabled

      $tpl = $btn.Template
      if ($tpl) {
        $border  = $tpl.FindName("heroBd",      $btn)
        $spinner = $tpl.FindName("HeroSpinner", $btn)
        if ($border -and -not $SkipBrush) { $border.Background = (New-HeroBrush $variant) }
        if ($spinner) {
          if ($spin) { Start-SpinnerAnim $spinner } else { Stop-SpinnerAnim $spinner }
        }
      }

      # Language toggle is locked while a background script is running so the
      # button label can't change mid-flight (cosmetic guard).
      $ctrl.BtnLang.IsEnabled = ($State -eq "Off" -or $State -eq "Ready")
    } catch {
      $st = $_.ScriptStackTrace; if (-not $st) { $st = "" }
      Write-BootstrapLog "Set-ButtonState dispatcher error: $($_.Exception.Message)`n  $st"
    }
  })
}

# ---- Wi-Fi panel refresh ---------------------------------------------------

function Update-WifiPanel {
  $info = Get-HostIPv4
  $script:lastIp     = $info.IP
  $script:lastIpMode = $info.Mode
  $window.Dispatcher.Invoke([action]{
    try {
      $ctrl.TxtSsid.Text     = $ssid
      $ctrl.TxtPassword.Text = $key
      $showIp = $info.IP
      if (-not $showIp) {
        $ctrl.TxtIPv4.Text       = (T "ipUnavailable")
        $ctrl.TxtIPv4.Foreground = [System.Windows.Media.Brushes]::Gray
      } else {
        $ctrl.TxtIPv4.Text = $showIp
        if ($info.Mode -eq "hotspot") {
          $ctrl.TxtIPv4.Foreground = (New-Object System.Windows.Media.SolidColorBrush ([System.Windows.Media.Color]::FromRgb(27, 79, 156)))
        } else {
          $ctrl.TxtIPv4.Foreground = (New-Object System.Windows.Media.SolidColorBrush ([System.Windows.Media.Color]::FromRgb(71, 85, 105)))
        }
      }
    } catch {
      $st = $_.ScriptStackTrace; if (-not $st) { $st = "" }
      Write-BootstrapLog "Update-WifiPanel dispatcher error: $($_.Exception.Message)`n  $st"
    }
  })
}

# ---- Footer ---------------------------------------------------------------

function Update-Footer {
  param([string]$Error = "")
  $window.Dispatcher.Invoke([action]{
    try {
      if ($Error) {
        $ctrl.TxtFooter.Text = (TF "footerError" $Error)
        return
      }
      switch ($script:state) {
        "Off"      { $ctrl.TxtFooter.Text = (T "footerOff") }
        "Starting" { $ctrl.TxtFooter.Text = (T "footerStarting") }
        "Ready"    { $ctrl.TxtFooter.Text = (TF "footerReady" $hostname) }
        "Stopping" { $ctrl.TxtFooter.Text = (T "footerStopping") }
      }
    } catch {
      Write-BootstrapLog "Update-Footer error: $($_.Exception.Message)"
    }
  })
}

# ---- Background script runner ---------------------------------------------

function Start-BackgroundScript {
  param(
    [string]$Path,
    [scriptblock]$OnSuccess,
    [scriptblock]$OnFailure
  )
  if ($script:activeJob) { return }
  if (-not (Test-Path $Path)) {
    $err = "script not found: $Path"
    Write-BootstrapLog "ERROR Start-BackgroundScript: $err"
    if ($OnFailure) { & $OnFailure $err }
    return
  }
  Write-BootstrapLog "----- start: $([System.IO.Path]::GetFileName($Path)) -----"

  $rs = $null
  $ps = $null
  $handle = $null

  try {
    $rs = [runspacefactory]::CreateRunspace()
    $rs.ApartmentState = "STA"
    $rs.Open()
    $rs.SessionStateProxy.SetVariable("uiPath",       $Path)
    $rs.SessionStateProxy.SetVariable("uiWindow",     $window)
    $rs.SessionStateProxy.SetVariable("uiLogFile",    $bootstrapLogFile)
    $rs.SessionStateProxy.SetVariable("uiLogDir",     $bootstrapLogDir)

    $ps = [powershell]::Create()
    $ps.Runspace = $rs
    [void]$ps.AddScript({
      function Write-SubLog {
        param([string]$Tag, [string]$Line)
        try {
          if (-not (Test-Path $uiLogDir)) { New-Item -ItemType Directory -Path $uiLogDir -Force | Out-Null }
          $ts = Get-Date -Format "yyyy-MM-ddTHH:mm:ssK"
          Add-Content -LiteralPath $uiLogFile -Value ("{0} [{1}] {2}" -f $ts, $Tag, $Line) -Encoding UTF8
        } catch {}
      }

      $psi = New-Object System.Diagnostics.ProcessStartInfo
      $psi.FileName = "powershell.exe"
      $psi.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $uiPath + '"'
      $psi.UseShellExecute = $false
      $psi.RedirectStandardOutput = $true
      $psi.RedirectStandardError = $true
      $psi.CreateNoWindow = $true

      $proc = New-Object System.Diagnostics.Process
      $proc.StartInfo = $psi
      $proc.EnableRaisingEvents = $true

      $outHandler = [System.Diagnostics.DataReceivedEventHandler]{
        param($sender, $args)
        if ([string]::IsNullOrEmpty($args.Data)) { return }
        Write-SubLog "stdout" $args.Data
      }
      $errHandler = [System.Diagnostics.DataReceivedEventHandler]{
        param($sender, $args)
        if ([string]::IsNullOrEmpty($args.Data)) { return }
        Write-SubLog "stderr" $args.Data
      }

      $proc.add_OutputDataReceived($outHandler)
      $proc.add_ErrorDataReceived($errHandler)
      try {
        [void]$proc.Start()
        $proc.BeginOutputReadLine()
        $proc.BeginErrorReadLine()
        $proc.WaitForExit()
        # Flush any buffered async event callbacks.
        $proc.WaitForExit()
        return $proc.ExitCode
      } finally {
        try { $proc.CancelOutputRead() } catch {}
        try { $proc.CancelErrorRead() } catch {}
        try { $proc.remove_OutputDataReceived($outHandler) } catch {}
        try { $proc.remove_ErrorDataReceived($errHandler) } catch {}
        try { $proc.Dispose() } catch {}
      }
    })

    $handle = $ps.BeginInvoke()
  } catch {
    $errMsg = $_.Exception.Message
    Write-BootstrapLog "----- start failed: $errMsg -----"
    try { if ($ps) { $ps.Dispose() } } catch {}
    try { if ($rs) { $rs.Close(); $rs.Dispose() } } catch {}
    $script:activeJob = $null
    if ($OnFailure) { & $OnFailure $errMsg }
    return
  }

  # Job is stored at script scope so the timer's Add_Tick scriptblock (which
  # runs in the dispatcher's session state) can see it. The legacy code used
  # $script:activeJobs[0]; we use a single $script:activeJob, eliminating the
  # array re-entrancy hazard.
  $script:activeJob = [pscustomobject]@{
    PS        = $ps
    Handle    = $handle
    RS        = $rs
    Path      = $Path
    OnSuccess = $OnSuccess
    OnFailure = $OnFailure
  }

  $script:activeTimer = New-Object System.Windows.Threading.DispatcherTimer
  $script:activeTimer.Interval = [TimeSpan]::FromMilliseconds(300)
  $script:activeTimer.Add_Tick({
    try {
      $j = $script:activeJob
      $t = $script:activeTimer
      if (-not $j -or -not $j.Handle) { if ($t) { $t.Stop() }; return }
      if (-not $j.Handle.IsCompleted) { return }
      if ($t) { $t.Stop() }

      $exitCode = -1
      $threwInsideRunspace = $false
      try {
        $exitRaw = $j.PS.EndInvoke($j.Handle)
        $exitItems = @($exitRaw)
        if ($exitItems.Count -gt 0) {
          try { $exitCode = [int]$exitItems[-1] } catch { $exitCode = -1 }
        } else {
          $exitCode = 0
        }
        Write-BootstrapLog ("----- finished {0} exit={1} -----" -f ([System.IO.Path]::GetFileName($j.Path)), $exitCode)
      } catch {
        $threwInsideRunspace = $true
        Write-BootstrapLog ("----- threw {0}: {1} -----" -f ([System.IO.Path]::GetFileName($j.Path)), $_.Exception.Message)
      } finally {
        try { if ($j.PS) { $j.PS.Dispose() } } catch {}
        try { if ($j.RS) { $j.RS.Close(); $j.RS.Dispose() } } catch {}
        $script:activeJob = $null
        $script:activeTimer = $null
      }

      try {
        if (-not $threwInsideRunspace -and $exitCode -eq 0) {
          if ($j.OnSuccess) { & $j.OnSuccess }
        } else {
          $msg = if ($threwInsideRunspace) { "runspace exception" } else { "exit code $exitCode" }
          if ($j.OnFailure) { & $j.OnFailure $msg }
        }
      } catch {
        $st = $_.ScriptStackTrace; if (-not $st) { $st = "" }
        Write-BootstrapLog "Completion callback error: $($_.Exception.Message)`n  $st"
      }
    } catch {
      $st = $_.ScriptStackTrace; if (-not $st) { $st = "" }
      Write-BootstrapLog "Completion timer error: $($_.Exception.Message)`n  $st"
    }
  })
  $script:activeTimer.Start()
}

# ---- Event handlers --------------------------------------------------------

$ctrl.BtnPrimary.Add_Click({
  try {
    switch ($script:state) {
      "Off" {
        Set-ButtonState -State "Starting"
        Update-Footer
        Start-BackgroundScript -Path $upScript `
          -OnSuccess { Set-ButtonState -State "Ready"; Update-WifiPanel; Update-Footer } `
          -OnFailure {
            param($err)
            Set-ButtonState -State "Off"
            Update-Footer -Error $err
          }
      }
      "Ready" {
        Set-ButtonState -State "Stopping"
        Update-Footer
        Start-BackgroundScript -Path $downScript `
          -OnSuccess { Set-ButtonState -State "Off"; Update-WifiPanel; Update-Footer } `
          -OnFailure {
            param($err)
            Set-ButtonState -State "Off"
            Update-WifiPanel
            Update-Footer -Error $err
          }
      }
      default { return }
    }
  } catch {
    $st = $_.ScriptStackTrace; if (-not $st) { $st = "" }
    Write-BootstrapLog "BtnPrimary handler error: $($_.Exception.Message)`n  $st"
  }
})

$ctrl.BtnLang.Add_Click({
  try {
    if ($script:language -eq "es") { $script:language = "en" } else { $script:language = "es" }
    Set-LanguageLabels
    Save-Prefs @{
      language     = $script:language
      windowWidth  = $window.Width
      windowHeight = $window.Height
    }
  } catch {
    $st = $_.ScriptStackTrace; if (-not $st) { $st = "" }
    Write-BootstrapLog "BtnLang handler error: $($_.Exception.Message)`n  $st"
  }
})

$window.Add_Closing({
  param($sender, $e)
  try {
    if ($script:state -eq "Starting" -or $script:state -eq "Stopping") {
      $msg    = (T "confirmCloseBody")
      $title  = (T "confirmCloseTitle")
      $result = [System.Windows.MessageBox]::Show(
        $window, $msg, $title,
        [System.Windows.MessageBoxButton]::YesNo,
        [System.Windows.MessageBoxImage]::Warning,
        [System.Windows.MessageBoxResult]::No)
      if ($result -ne [System.Windows.MessageBoxResult]::Yes) {
        $e.Cancel = $true
        return
      }
    }
    Save-Prefs @{
      language     = $script:language
      windowWidth  = $window.Width
      windowHeight = $window.Height
    }
  } catch {
    Write-BootstrapLog "Closing handler error: $($_.Exception.Message)"
  }
})

Register-DispatcherUnhandledExceptionHandler -Dispatcher $window.Dispatcher

# ---- Status poll timer -----------------------------------------------------

$statusTimer = New-Object System.Windows.Threading.DispatcherTimer
$statusTimer.Interval = [TimeSpan]::FromSeconds(3)
$statusTimer.Add_Tick({
  try {
    # If a background script is in flight, skip the auto-state-sync (the click
    # handler's OnSuccess/OnFailure is the source of truth during that window).
    if ($script:state -eq "Starting" -or $script:state -eq "Stopping") {
      Update-WifiPanel
      return
    }
    Update-WifiPanel

    # If the hotspot is live but the UI thinks it's Off (e.g. user opened the
    # console after the scheduled task started the stack), flip to Ready.
    # Conversely, if the UI thinks it's Ready but the hotspot adapter is gone,
    # flip to Off. We treat $script:lastIpMode == "hotspot" as the indicator.
    if ($script:state -eq "Off" -and $script:lastIpMode -eq "hotspot") {
      Set-ButtonState -State "Ready"
      Update-Footer
    } elseif ($script:state -eq "Ready" -and $script:lastIpMode -ne "hotspot") {
      Set-ButtonState -State "Off"
      Update-Footer
    }
  } catch {
    $st = $_.ScriptStackTrace; if (-not $st) { $st = "" }
    Write-BootstrapLog "Status timer error: $($_.Exception.Message)`n  $st"
  }
})

# ---- First paint -----------------------------------------------------------

Set-LanguageLabels
Set-ButtonState -State "Off"
Update-WifiPanel
Update-Footer

$window.Add_ContentRendered({
  if ($script:firstShowDone) { return }
  $script:firstShowDone = $true
  try {
    Write-BootstrapLog "Window content rendered"
    $window.Topmost = $true
    $window.Activate()
    $window.Topmost = $false
    $statusTimer.Start()
    # Re-probe the network immediately in case the stack was already up before
    # the UI opened — sets the button to Ready if so.
    Update-WifiPanel
    if ($script:lastIpMode -eq "hotspot") {
      Set-ButtonState -State "Ready"
      Update-Footer
    }
  } catch {
    $st = $_.ScriptStackTrace; if (-not $st) { $st = "" }
    Write-BootstrapLog "ContentRendered error: $($_.Exception.Message)`n  $st"
  }
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
