# Piper Reader browser extension

This unpacked Manifest V3 extension works in Google Chrome and Microsoft Edge.
It reads the rendered article in the current tab with the Piper Reader Windows
app, highlights each sentence as its audio starts, and adds a floating player.

## Install in Chrome

1. Keep Piper Reader running (its window may be hidden in the system tray).
2. Open `chrome://extensions`.
3. Turn on **Developer mode**.
4. Select **Load unpacked** and choose this `browser_extension` folder.
5. Pin **Piper Reader** from Chrome's Extensions menu if desired.

## Install in Microsoft Edge

1. Open `edge://extensions`.
2. Turn on **Developer mode**.
3. Select **Load unpacked** and choose this same `browser_extension` folder.

## Use

- Open a normal `http` or `https` webpage and click the Piper Reader extension.
- Use the floating play/pause, restart, stop, progress, and locate controls.
- Alt+click a sentence to start reading from that sentence.
- Closing the Piper Reader desktop window hides it in the Windows tray; webpage
  reading and clipboard auto-read continue to work.
- The desktop app's Repeat setting can repeat each sentence, webpage block, or
  complete page passage before advancing. Reload this unpacked extension after
  an update so paragraph grouping and repeat progress are current.

The extension requests access to a webpage only after its toolbar button is
clicked. The extension sends text to Piper Reader on `127.0.0.1`. Piper Reader
then uses the selected voice: local voices process it on the computer, while
online voices send it to the selected speech provider. Browser-internal pages, extension stores, and some
protected browser PDF viewers do not allow extension injection.
