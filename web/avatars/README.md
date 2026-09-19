# The assistant's face

The voice page draws a **built-in 3D persona** (three.js primitives in `web/avatar.js`) unless a GLB is configured, so
speech mode works with no asset at all.

To use a rigged avatar instead, put a `.glb` here and set `REALTIME_AVATAR_URL=/static/avatars/<file>.glb`
(Studio › Settings › Runtime, or the container app's environment).

Requirements, from the TalkingHead library that renders it:
- a Mixamo-compatible rig whose root is named `Armature`
- ARKit blend shapes **and** Oculus visemes (the mouth is driven by `viseme_*`)

Where to make one, now that Ready Player Me has closed (January 2026):
- **Avaturn** (https://hub.avaturn.me) - photo to a realistic avatar, free for non-commercial use; export GLB.
- **MPFB in Blender** (CC0 assets) - see https://github.com/met4citizen/TalkingHead/blob/main/blender/MPFB/MPFB.md
- **Microsoft RocketBox** (MIT) - needs re-rigging in Mixamo.

Keep the file under ~8 MB; it ships inside the container image. To shrink one:

    npx -y @gltf-transform/cli optimize in.glb tah.glb --compress false --texture-compress webp --texture-size 1024

Do not enable Draco or meshopt compression: the loader here has no decoder for them.
