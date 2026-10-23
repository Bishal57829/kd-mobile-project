[app]
title = KD Mobile
package.name = kdmobile
package.domain = org.kdmobile
source.dir = .
source.include_exts = py,png,jpg,kv
source.exclude_dirs = tests,.github,bin,.buildozer
version = 1.0.0
requirements = python3,kivy==2.3.0,requests,urllib3,idna,certifi,charset-normalizer
orientation = portrait
fullscreen = 0

[buildozer]
log_level = 2
warn_on_root = 1

[app:android]
android.permissions = INTERNET
android.api = 33
android.minapi = 24
android.archs = arm64-v8a, armeabi-v7a
android.accept_sdk_license = True
android.ndk = 25b
android.build_tools_version = 33.0.2
