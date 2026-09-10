# vhs script for the README GIF: https://github.com/charmbracelet/vhs
#
#   ./setup.sh          # once, deploys the demo
#   vhs demo.tape       # produces demo.gif
#
# Tune the Sleep below if your cluster applies resizes more slowly: it must
# outlast demo.sh (DURATION, 45s by default) plus a few seconds of margin.
Output demo.gif

Require kubectl

Set Shell "bash"
Set FontSize 16
Set Width 1300
Set Height 720
Set Padding 20
Set Framerate 12
Set PlaybackSpeed 1.5

Type "./demo.sh"
Enter
Sleep 55s
