# vhs script for the README GIF: https://github.com/charmbracelet/vhs
#
#   ./setup.sh          # once, deploys the demo
#   vhs demo.tape       # produces demo.gif
#
# Tune the Sleep below if your cluster applies resizes more slowly: it must
# outlast demo.sh (DURATION, 30s by default) plus a few seconds of margin.
#
# vhs renders through a headless Chromium that go-rod downloads on first run.
# Where that download cannot reach its host, vhs exits 0 having written
# nothing -- see RECORDING.md and render_gif.py for the fallback.
Output demo.gif

Require kubectl

Set Shell "bash"
Set FontSize 16
Set Width 1300
Set Height 760
Set Padding 20
Set Framerate 12
Set PlaybackSpeed 2

Type "./demo.sh"
Enter
Sleep 40s
