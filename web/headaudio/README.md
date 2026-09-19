# HeadAudio

Audio-driven viseme detection in an AudioWorklet: it turns the assistant's own voice into mouth shapes, with no text,
no timings and no language model, which is why the lip-sync follows Thai as well as English.

- Source: https://github.com/met4citizen/HeadAudio (MIT, see LICENSE), npm `@met4citizen/headaudio` 0.1.0
- `headaudio.min.mjs` the AudioWorkletNode, `headworklet.min.mjs` the processor, `model-en-mixed.bin` the viseme model.
- Served from /static/headaudio/ and loaded by web/avatar.js. Vendored rather than loaded from a CDN because the
  worklet and its model must come from the app's own origin.
