# Olde TV

Plays the videos in `/video` full screen on the Pi's HDMI output at SD resolution
(720x576@50 PAL by default, or 720x480@60 NTSC),
straight to the KMS/DRM framebuffer (no desktop needed), for an HDMI -> RF
modulator. Each sub-directory of `/video` is a channel; GPIO buttons change channel.

```
/video/
  01-cartoons/   ep1.mp4 ep2.mkv ...     -> channel 1
  02-news/       ...                     -> channel 2
  03-movies/     ...                     -> channel 3
```
Channels and files are sorted naturally (`2` before `10`). Sub-folders inside a
channel are included. Rescanned on every channel change, so new files appear
without restarting.

## Buttons (BCM numbering, wire each button between the pin and GND)
| Function     | GPIO   | Header pin | Ground pin |
|--------------|--------|------------|------------|
| Channel up   | GPIO17 | 11         | 9          |
| Channel down | GPIO27 | 13         | 14         |

Hold a button to keep scanning. Change pins with `--up-pin` / `--down-pin`.
From a shell: `pkill -USR1 -f olde_tv.py` (up), `pkill -USR2 -f olde_tv.py` (down).
When run in a terminal you can also type `+`, `-` or a channel number + Enter.

## Run
```
python3 olde_tv.py                    # live TV schedule (default)
python3 olde_tv.py --schedule resume  # each channel resumes where you left it
python3 olde_tv.py --standard ntsc   # 720x480@60 for an NTSC modulator
python3 olde_tv.py --overscan 5      # shrink picture 5% per side if the TV crops edges
python3 olde_tv.py --osd-names        # show folder name under the channel number
python3 olde_tv.py --tv-aspect 16:9   # widescreen TV
python3 olde_tv.py --help
```
* **live**: channels keep "broadcasting" while you're watching something else.
  Position comes from the wall clock, so it's consistent across reboots.
* **resume**: picks up exactly where you left each channel.

Last channel and positions are stored in `~/.cache/olde-tv/`.

## Start at boot
```
sudo cp olde-tv.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now olde-tv
journalctl -u olde-tv -f
```

## PAL vs NTSC
Match `--standard` to what the RF modulator outputs. If you feed it the wrong
one it rescales the picture, and the edges get cut off. Many modulators have a
PAL/NTSC switch or menu. If the picture is still a little too big after that,
that's normal CRT overscan: use `--overscan 3` to `8`.

## Positioning the picture
Some modulators take a PAL (720x576) signal but only show an NTSC-sized area
of it on the TV, so the picture runs off the right or bottom. To draw the video
in a 720x480 box in the top left corner of the PAL output:
```
python3 olde_tv.py --canvas 720x480
```
To fine-tune, move the picture in pixels (negative = left / up):
```
python3 olde_tv.py --canvas 720x480 --offset-x -8 --offset-y -4
```
The aspect correction follows the box, so 4:3 video still looks 4:3 on the TV,
and the channel number stays inside the box. `--overscan` shrinks the picture
inside the box.

## Forcing the mode when the RF modulator has no EDID
mpv sets the mode itself (`--drm-mode`), but it can only pick modes the kernel
offers. Many cheap modulators don't send EDID, so the mode may be missing or the
port reported as disconnected (the script logs a warning). Fix it on the kernel
command line: append to the single line in `/boot/firmware/cmdline.txt`
```
video=HDMI-A-1:720x576@50D      # PAL
video=HDMI-A-1:720x480@60D      # NTSC
```
(`HDMI-A-1` = the HDMI port next to USB-C, `HDMI-A-2` = the other one; `D` forces
the output on.) This also sets the boot console to that mode. Reboot afterwards.
Note: legacy `hdmi_group`/`hdmi_mode` in config.txt are ignored under the
`vc4-kms-v3d` driver.

## Notes
* Hardware decoding uses `v4l2m2m-copy` (H.264 on the Pi 4); other codecs fall
  back to software, which is fine at SD resolution. H.264 files are the safest choice.
* Audio goes to the matching HDMI port (`--audio-device` to override).
* The OSD channel number sits inside the CRT safe area.
