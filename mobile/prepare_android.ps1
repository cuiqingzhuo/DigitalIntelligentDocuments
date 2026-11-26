param([string]$Flutter = 'flutter', [switch]$Refresh)

$ErrorActionPreference = 'Stop'
$mobile = $PSScriptRoot
$root = Split-Path -Parent $mobile
$scratch = Join-Path $root 'runtime/flutter-android-template'
$android = Join-Path $mobile 'android'
$absoluteFlutter = (Get-Command $Flutter -ErrorAction Stop).Source

if (-not (Test-Path -LiteralPath (Join-Path $android 'gradlew.bat')) -or $Refresh) {
    New-Item -ItemType Directory -Path $scratch -Force | Out-Null
    & $absoluteFlutter create --platforms=android --project-name diw_reader --org cn.diw --no-pub $scratch
    if ($LASTEXITCODE -ne 0) { throw 'Flutter Android 模板生成失败' }
    New-Item -ItemType Directory -Path $android -Force | Out-Null
    Get-ChildItem -LiteralPath (Join-Path $scratch 'android') -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $android -Recurse -Force
    }
    Copy-Item -LiteralPath (Join-Path $scratch '.metadata') -Destination $mobile -Force
}

$kotlin = Join-Path $android 'app/src/main/kotlin/cn/diw/diw_reader/MainActivity.kt'
Copy-Item -LiteralPath (Join-Path $mobile 'android-native/MainActivity.kt') -Destination $kotlin -Force
$gradle = Join-Path $android 'app/build.gradle.kts'
$text = Get-Content -LiteralPath $gradle -Raw
$text = $text -replace 'minSdk\s*=\s*flutter\.minSdkVersion', 'minSdk = 24'
if ($text -notmatch 'com.google.mlkit:text-recognition-chinese') {
    $text += @'

dependencies {
    implementation("com.google.mlkit:text-recognition-chinese:16.0.1")
    implementation("com.google.mlkit:barcode-scanning:17.3.0")
    implementation("androidx.exifinterface:exifinterface:1.4.2")
}
'@
}
Set-Content -LiteralPath $gradle -Value $text -Encoding utf8
$manifest = Join-Path $android 'app/src/main/AndroidManifest.xml'
$xml = Get-Content -LiteralPath $manifest -Raw -Encoding utf8
if ($xml -notmatch 'android.permission.INTERNET') {
    $xml = $xml -replace '<application', '<uses-permission android:name="android.permission.INTERNET"/><application'
}
$xml = $xml -replace 'android:label="diw_reader"', 'android:label="数智文枢"'
if ($xml -notmatch 'android:allowBackup=') {
    $xml = $xml -replace 'android:label="数智文枢"', 'android:label="数智文枢" android:allowBackup="false"'
}
$xml = $xml -replace '@mipmap/ic_launcher', '@drawable/diw_launcher'
if ($xml -notmatch 'android:scheme="diw"') {
    $intent = @'
            <meta-data android:name="flutter_deeplinking_enabled" android:value="false"/>
            <intent-filter>
                <action android:name="android.intent.action.VIEW"/>
                <category android:name="android.intent.category.DEFAULT"/>
                <category android:name="android.intent.category.BROWSABLE"/>
                <data android:scheme="diw" android:host="shelf"/>
            </intent-filter>
        </activity>
'@
    $xml = $xml -replace '        </activity>', $intent
}
Set-Content -LiteralPath $manifest -Value $xml -Encoding utf8
Copy-Item -LiteralPath (Join-Path $mobile 'android-native/diw_launcher.xml') -Destination (Join-Path $android 'app/src/main/res/drawable/diw_launcher.xml') -Force
$debug = Join-Path $android 'app/src/debug/AndroidManifest.xml'
$debugManifest = @'
<manifest xmlns:android="http://schemas.android.com/apk/res/android">
    <application android:usesCleartextTraffic="true"/>
</manifest>
'@
Set-Content -LiteralPath $debug -Value $debugManifest -Encoding utf8
Write-Output 'Android 原生桥接已准备就绪'
