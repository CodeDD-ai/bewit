param(
  [Parameter(Mandatory)] [string] $Url,
  [Parameter(Mandatory)] [string] $Name,
  [string] $OutDir = (Join-Path $env:TEMP "codedd-shots"),
  [int[]] $Widths = @(1280, 375),
  [int] $ChunkHeight = 900,
  [int] $Port = 9333,
  # Emulate prefers-reduced-motion: animated widgets (e.g. the Advisor preview) render their end state.
  [switch] $ReducedMotion,
  # JavaScript run after load, before capture (e.g. open a menu). Its return value is printed.
  [string] $Script
)
# Full-page screenshots at any width via headless Edge + DevTools device emulation,
# cut into screen-sized sections. Reports horizontal overflow per width.
# Waits for the page footer (so a recompiling dev server is not captured half-rendered) and
# retries a width once, with a fresh Edge, on a DevTools error.
# Needs only Microsoft Edge and .NET (System.Drawing, ClientWebSocket) - no installs.
Add-Type -AssemblyName System.Drawing
$ErrorActionPreference = "Stop"
$edge = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
if (-not (Test-Path $edge)) { throw "Microsoft Edge not found at $edge" }
New-Item -ItemType Directory -Force $OutDir | Out-Null

# One profile per port: Edge hands a second launch on the same profile to the
# running instance, so parallel sessions would capture (and kill) each other.
$profileDir = Join-Path $env:TEMP "edge-shot-cdp-$Port"
$script:proc = $null
$script:ws = $null
$script:nextId = 0

function Stop-Edge {
  if ($script:ws) { try { $script:ws.Dispose() } catch {} ; $script:ws = $null }
  Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" |
    Where-Object { $_.CommandLine -like "*$profileDir*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

function Start-Edge {
  Stop-Edge
  # Let the previous Edge release the DevTools port before the next one binds it.
  Start-Sleep -Milliseconds 1200
  $script:proc = Start-Process $edge -PassThru -WindowStyle Hidden -ArgumentList @(
    "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
    "--remote-debugging-port=$Port", "--user-data-dir=$profileDir", "about:blank"
  )
  $target = $null
  for ($try = 0; $try -lt 40 -and -not $target; $try++) {
    Start-Sleep -Milliseconds 250
    try {
      $target = (Invoke-RestMethod "http://127.0.0.1:$Port/json/list") | Where-Object { $_.type -eq "page" } | Select-Object -First 1
    } catch {}
  }
  if (-not $target) { throw "Could not reach Edge DevTools on port $Port" }
  $script:ws = New-Object Net.WebSockets.ClientWebSocket
  $script:ws.ConnectAsync([Uri] $target.webSocketDebuggerUrl, [Threading.CancellationToken]::None).GetAwaiter().GetResult()
}

function Receive-Message {
  $buffer = New-Object byte[] 1048576
  $ms = New-Object System.IO.MemoryStream
  do {
    $seg = New-Object System.ArraySegment[byte] -ArgumentList (, $buffer)
    $res = $script:ws.ReceiveAsync($seg, [Threading.CancellationToken]::None).GetAwaiter().GetResult()
    $ms.Write($buffer, 0, $res.Count)
  } while (-not $res.EndOfMessage)
  [Text.Encoding]::UTF8.GetString($ms.ToArray()) | ConvertFrom-Json
}

function Invoke-Cdp([string] $Method, [hashtable] $Params = @{}) {
  $script:nextId++
  $id = $script:nextId
  $json = @{ id = $id; method = $Method; params = $Params } | ConvertTo-Json -Depth 8 -Compress
  $bytes = [Text.Encoding]::UTF8.GetBytes($json)
  $seg = New-Object System.ArraySegment[byte] -ArgumentList (, $bytes)
  $script:ws.SendAsync($seg, [Net.WebSockets.WebSocketMessageType]::Text, $true, [Threading.CancellationToken]::None).GetAwaiter().GetResult()
  while ($true) {
    $msg = Receive-Message
    if ($msg.id -eq $id) {
      if ($msg.error) { throw "$Method failed: $($msg.error.message)" }
      return $msg.result
    }
  }
}

function Get-JsValue([string] $Expression) {
  (Invoke-Cdp "Runtime.evaluate" @{ expression = $Expression; returnByValue = $true; awaitPromise = $true }).result.value
}

function Test-Blank([System.Drawing.Bitmap] $bmp) {
  $ref = $bmp.GetPixel(0, 0)
  for ($gx = 1; $gx -le 19; $gx++) {
    for ($gy = 1; $gy -le 9; $gy++) {
      if ($bmp.GetPixel([int]($bmp.Width * $gx / 20), [int](($bmp.Height - 1) * $gy / 10)) -ne $ref) { return $false }
    }
  }
  return $true
}

function Capture-Width([int] $w) {
  $mobile = $w -lt 768
  Invoke-Cdp "Emulation.setDeviceMetricsOverride" @{ width = $w; height = 900; deviceScaleFactor = 1; mobile = $mobile } | Out-Null
  if ($ReducedMotion) {
    Invoke-Cdp "Emulation.setEmulatedMedia" @{ features = @(@{ name = "prefers-reduced-motion"; value = "reduce" }) } | Out-Null
  }
  Invoke-Cdp "Page.navigate" @{ url = $Url } | Out-Null
  for ($try = 0; $try -lt 60 -and (Get-JsValue "document.readyState") -ne "complete"; $try++) { Start-Sleep -Milliseconds 250 }
  # A recompiling dev server can serve a partial page first: wait until the footer is there.
  for ($try = 0; $try -lt 60 -and -not (Get-JsValue "!!document.querySelector('footer')"); $try++) { Start-Sleep -Milliseconds 250 }
  # A fresh Edge can lay out the first page before the emulated width applies: verify, then reload.
  for ($try = 0; $try -lt 3 -and [int](Get-JsValue "window.innerWidth") -ne $w; $try++) {
    Invoke-Cdp "Emulation.setDeviceMetricsOverride" @{ width = $w; height = 900; deviceScaleFactor = 1; mobile = $mobile } | Out-Null
    Invoke-Cdp "Page.reload" @{ ignoreCache = $true } | Out-Null
    Start-Sleep -Milliseconds 2500
  }
  $actual = [int](Get-JsValue "window.innerWidth")
  if ($actual -ne $w) { throw "viewport is ${actual}px, expected ${w}px" }
  Start-Sleep -Milliseconds 1500

  # Walk the page once so scroll-triggered reveals fire, then return to the top.
  Get-JsValue "(async () => { for (let y = 0; y < document.documentElement.scrollHeight; y += 600) { window.scrollTo({ top: y, behavior: 'instant' }); await new Promise(r => setTimeout(r, 60)); } window.scrollTo({ top: 0, behavior: 'instant' }); return true; })()" | Out-Null

  if ($Script) {
    $result = Get-JsValue $Script
    Write-Output ("$w px script: " + ($result | ConvertTo-Json -Compress -Depth 6))
    Start-Sleep -Milliseconds 600
  }
  $height = [int](Get-JsValue "document.documentElement.scrollHeight")
  $overflow = [int](Get-JsValue "document.documentElement.scrollWidth - window.innerWidth")
  Invoke-Cdp "Emulation.setDeviceMetricsOverride" @{ width = $w; height = $height; deviceScaleFactor = 1; mobile = $mobile } | Out-Null
  Start-Sleep -Milliseconds 1200

  $shot = Invoke-Cdp "Page.captureScreenshot" @{ format = "png" }
  $full = Join-Path $OutDir "$Name-$w-full.png"
  [IO.File]::WriteAllBytes($full, [Convert]::FromBase64String($shot.data))

  # Clear old sections for this name/width so a shorter page leaves no stale files behind.
  Get-ChildItem $OutDir -Filter "$Name-$w-??.png" -ErrorAction SilentlyContinue | Remove-Item -Force
  $img = [System.Drawing.Bitmap]::FromFile($full)
  $chunk = if ($mobile) { [int]($ChunkHeight * 1.2) } else { $ChunkHeight }
  $i = 0
  for ($y = 0; $y -lt $img.Height; $y += $chunk) {
    $h = [Math]::Min($chunk, $img.Height - $y)
    $rect = New-Object System.Drawing.Rectangle 0, $y, $img.Width, $h
    $part = $img.Clone($rect, $img.PixelFormat)
    if (Test-Blank $part) { $part.Dispose(); continue }
    $out = Join-Path $OutDir ("{0}-{1}-{2:D2}.png" -f $Name, $w, $i)
    $part.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
    $part.Dispose()
    Write-Output $out
    $i++
  }
  $img.Dispose()
  $flag = if ($overflow -gt 0) { "HORIZONTAL OVERFLOW ${overflow}px" } else { "no horizontal overflow" }
  Write-Output "$w px: page height $height, $i sections, $flag"
}

try {
  foreach ($w in $Widths) {
    try {
      # A fresh Edge per width: long sessions with multi-MB screenshots drop the DevTools socket.
      Start-Edge
      Capture-Width $w
    } catch {
      Write-Warning "$w px: $($_.Exception.Message) - restarting Edge and retrying once"
      Start-Edge
      try { Capture-Width $w } catch { Write-Output "$w px: FAILED - $($_.Exception.Message)" }
    }
  }
} finally {
  Stop-Edge
}
