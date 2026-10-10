XCover Root Manager test APK source.
Upload ALL contents of this directory to the ROOT of a separate GitHub repository, or merge the files at the root of your current repo. Existing kernel workflow remains unchanged.
Actions -> Build XCover Root Manager APK -> Run workflow. Download XCoverRootManager-TEST-APK artifact.
The app talks to /data/local/rootmanager/control.sock and expects Rootbroker protocol PENDING/LIST/DENIED/ALLOW/DENY/ASK.
It does not install Rootbroker, su, or modify the boot image. It cannot work before a compatible Rootbroker is running.
WARNING: Rootbroker uses UID-only allow/deny lists; app UID reuse after uninstall is unsafe. Do not flash until rootbroker identity and boot behavior are verified. Debug APK is test-only and signed with ephemeral debug key.
