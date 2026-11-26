# 数智文枢 Android

Flutter 3.50.1、Dart 3.14、JDK 21 和 Android SDK 36 用于本次构建。交付包支持 Android 7.0（API 24）及以上的 arm64 和 x86_64 设备，识别和扫码的模型随包提供。[安装包与校验信息](../release/README.md)

```powershell
cd mobile
.\prepare_android.ps1
flutter pub get
flutter analyze
flutter test
flutter build apk --debug --target-platform android-arm64,android-x64
```

安装包在 `build/app/outputs/flutter-apk/app-debug.apk`。可通过 `adb install -r` 安装到连接的真机或模拟器。在应用设置中输入服务地址。调试构建允许局域网 HTTP，试点发布使用 HTTPS、各馆正式令牌及自有签名。

在配置 Android SDK 后，`prepare_android.ps1` 会按需生成 Gradle 包装器，并同步 `android-native/` 下的相机推理桥接与启动资源。本机推理对单帧做书脊分割，尝试横排或旋转文字识别，在缓存书目上评分。服务同步的是已显示的判定与必要的可见文字，单独授权的照片可以送服务器复核。

原生相机及 OCR 接口参考 [Flutter 相机采集](https://pub.dev/packages/image_picker)、[ML Kit 中文文字识别](https://developers.google.com/ml-kit/vision/text-recognition/v2/android)、[Flutter Android 构建](https://docs.flutter.dev/deployment/android)。
