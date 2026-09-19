// The face of the voice call. Two drivers behind one interface:
//
//   glb        - a rigged avatar (TalkingHead + three.js) when REALTIME_AVATAR_URL points at a GLB with ARKit/Oculus
//                blend shapes, e.g. one exported from Avaturn.
//   procedural - a toy-style banker (a clay app-icon look) built from primitives, so the call has a face on the very
//                first run and when the GLB or the CDN is unreachable. No asset, no licence, no download.
//
// Both mouths are driven by the same signal: HeadAudio (MIT) classifies the *audio coming back from the model* into
// Oculus visemes in an AudioWorklet, roughly 50 ms behind the sound. Nothing needs the text, the timings or the
// language, which is why Thai lip-sync works at all - it reads the waveform, not the words.
import * as THREE from "three";

const HEADAUDIO_DIR = "/static/headaudio";
// Oculus visemes -> how this face holds its mouth: how far it opens, how wide it spreads, how far it rounds.
const SHAPES = {
  viseme_aa: [1.0, 0.35, 0.0], viseme_E: [0.5, 0.75, 0.0], viseme_I: [0.3, 0.9, 0.0],
  viseme_O: [0.7, 0.05, 0.85], viseme_U: [0.35, 0.0, 1.0], viseme_PP: [0.03, 0.15, 0.2],
  viseme_SS: [0.12, 0.6, 0.0], viseme_TH: [0.3, 0.4, 0.0], viseme_DD: [0.35, 0.4, 0.05],
  viseme_FF: [0.15, 0.35, 0.1], viseme_kk: [0.4, 0.35, 0.0], viseme_nn: [0.2, 0.4, 0.0],
  viseme_RR: [0.35, 0.25, 0.4], viseme_CH: [0.3, 0.2, 0.55], viseme_sil: [0.0, 0.0, 0.0],
};

async function headAudioFor(audioCtx) {
  const { HeadAudio } = await import(`${HEADAUDIO_DIR}/headaudio.min.mjs`);
  await audioCtx.audioWorklet.addModule(`${HEADAUDIO_DIR}/headworklet.min.mjs`);
  const ha = new HeadAudio(audioCtx);
  await ha.loadModel(`${HEADAUDIO_DIR}/model-en-mixed.bin`);
  return ha;
}

// ---------------- the built-in banker ----------------
// A toy-style 3D character in the manner of a clay app icon: one big smooth head, dot eyes, thick brows, a sweep of
// navy hair, a suit with rounded shoulders, and a glossy finish. Every part is a primitive, so it draws in a frame with
// no asset, and the style forgives simple geometry the way a realistic face never does.
function proceduralDriver(container, { gender = "male", accent = "#1f5fd6" } = {}) {
  const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  const scene = new THREE.Scene();
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping; renderer.toneMappingExposure = 1.05;
  container.appendChild(renderer.domElement);
  const camera = new THREE.PerspectiveCamera(24, 1, 0.1, 40);

  // soft studio light: the clay look comes from broad, even light and a reflected room, not from hard shadows
  scene.add(new THREE.HemisphereLight(0xffffff, 0x8fa3c4, 0.75));
  const key = new THREE.DirectionalLight(0xfff3e6, 1.9); key.position.set(1.2, 2.2, 3); scene.add(key);
  const fill = new THREE.DirectionalLight(0xdbe8ff, 0.9); fill.position.set(-2.5, 0.5, 2); scene.add(fill);
  const rim = new THREE.DirectionalLight(0xbfd4ff, 1.2); rim.position.set(-1, 1.5, -2.5); scene.add(rim);
  import("three/addons/environments/RoomEnvironment.js").then(({ RoomEnvironment }) => {
    const pmrem = new THREE.PMREMGenerator(renderer);
    scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
    scene.environmentIntensity = 0.45;
  }).catch(() => {});

  const clay = (color, extra = {}) => new THREE.MeshPhysicalMaterial({ color, roughness: 0.5, metalness: 0, clearcoat: 0.35, clearcoatRoughness: 0.45, ...extra });
  const skin = clay(0xe9b48f, { roughness: 0.6, sheen: 0.25, sheenColor: new THREE.Color(0xffc9b0) });
  const hairMat = clay(0x171d2b, { roughness: 0.38, clearcoat: 0.85, clearcoatRoughness: 0.22, sheen: 0.4, sheenColor: new THREE.Color(0x6f86b5) });
  // the tie takes the app's accent colour, so the persona wears the same blue as the interface around him
  const rgb = (hex, k = 1) => {
    const n = parseInt(String(hex).trim().replace("#", ""), 16);
    return Number.isFinite(n) ? [((n >> 16) & 255) * k, ((n >> 8) & 255) * k, (n & 255) * k].map(Math.round) : [31, 95, 214];
  };
  const featureMat = clay(0x141b28, { roughness: 0.25, clearcoat: 0.8, clearcoatRoughness: 0.2 });
  const smileMat = clay(0x8f3f3c, { roughness: 0.5 });
  const mouthMat = new THREE.MeshStandardMaterial({ color: 0x2b0d11, roughness: 0.85 });
  const tongueMat = clay(0xb8635f, { roughness: 0.55, clearcoat: 0.4 });
  const blushMat = new THREE.MeshBasicMaterial({ color: 0xf3a39c, transparent: true, opacity: 0.55, depthWrite: false });

  const person = new THREE.Group(); scene.add(person);

  // ---- body: the suit is one surface, painted and relieved ----
  // Built out of overlapping panels, a suit on a round chest always shows its seams: the pieces float, clip, or bury
  // each other as the body curves away. So the torso is a single shoulder mass, and the jacket is DRAWN on it - the
  // same (x, y) functions give the texture its colours and the mesh its relief, so a lapel edge and its crease are
  // the same line, and nothing can drift off the body.
  const T = { sx: 0.76, sy: 0.66, sz: 0.44, y: -1.09 };
  const ss2 = (a0, b0, t) => { const k = Math.min(1, Math.max(0, (t - a0) / (b0 - a0))); return k * k * (3 - 2 * k); };
  const lerp = (a0, b0, t) => a0 + (b0 - a0) * Math.min(1, Math.max(0, t));
  // where the jacket's front edges run: the V is wide at the collar and closes at the button
  const vEdge = (y) => (womanSuit ? lerp(0.125, 0.028, ss2(-0.46, -1.0, y)) : lerp(0.128, 0.03, ss2(-0.46, -0.98, y)));
  const lapelW = (y) => (womanSuit ? lerp(0.125, 0.04, ss2(-0.46, -1.0, y)) : lerp(0.15, 0.055, ss2(-0.46, -1.0, y)));
  // A man wears a necktie down the shirt; a woman's uniform here is a blazer over a blouse with the bank's scarf
  // knotted at the neck, which is how Bangkok Bank staff actually dress - so the same painted suit serves both.
  // A woman's suit here is the same cut read differently: a lighter blazer with a sharper, narrower lapel over a
  // buttoned white shirt, and no necktie. Everything else - the wrap onto the chest, the creases, the welt - is shared.
  const womanSuit = gender === "female";
  const vneckStyle = false, scarfStyle = false;
  const tieHalf = (y) => (scarfStyle
    ? (y > -0.55 ? lerp(0.055, 0.05, ss2(-0.46, -0.55, y))                                // the knot of the scarf
      : y > -0.72 ? lerp(0.05, 0.075, ss2(-0.55, -0.7, y))                                // it widens as it falls
        : lerp(0.075, 0, ss2(-0.72, -0.8, y)))                                            // and ends short
    : (y > -0.575 ? lerp(0.05, 0.036, ss2(-0.47, -0.575, y))                              // the knot of the tie
      : y > -0.93 ? lerp(0.028, 0.052, ss2(-0.575, -0.91, y))                             // the blade
        : lerp(0.052, 0, ss2(-0.93, -0.985, y))));                                        // the point
  const onTie = (x, y) => (!womanSuit && y < -0.465 && y > (scarfStyle ? -0.8 : -0.985) && Math.abs(x) < tieHalf(y) ? 1 : 0);
  // the shirt's own buttons, down the middle of the placket, where a tie would otherwise hide them
  const onShirtButton = (x, y) => {
    if (!womanSuit) return 0;
    for (const cy of [-0.6, -0.76, -0.92]) {
      if ((x / 0.016) ** 2 + ((y - cy) / 0.016) ** 2 < 1) return 1;
    }
    return 0;
  };
  const onPlacket = (x, y) => (womanSuit && y < -0.47 && y > -1.02 && Math.abs(Math.abs(x) - 0.028) < 0.004 ? 1 : 0);
  // the scarf also runs along the neckline, the way a tied scarf sits over the blouse
  const onDrape = (x, y) => {
    if (!scarfStyle) return 0;
    const ax = Math.abs(x), inner = lerp(0.15, 0.05, ss2(-0.46, -0.8, y));
    return y < -0.44 && y > -0.82 && ax < inner && ax > inner - 0.05 ? 1 : 0;
  };
  const onShirt = (x, y) => (y < -0.45 && Math.abs(x) < vEdge(y) ? 1 : 0);
  // the shirt collar: a band either side of the knot, running down and out from the neck
  const onCollar = (x, y) => {
    const ax = Math.abs(x), t = ss2(-0.45, -0.6, y);
    if (scarfStyle) return y < -0.44 && y > -0.58 && ax > lerp(0.055, 0.075, t) && ax < lerp(0.15, 0.1, t) ? 1 : 0;
    return y < -0.44 && y > -0.62 && ax > lerp(0.035, 0.055, t) && ax < lerp(0.125, 0.075, t) ? 1 : 0;
  };
  const onLapel = (x, y) => {
    const ax = Math.abs(x);
    return y < -0.44 && y > -1.12 && ax > vEdge(y) && ax < vEdge(y) + lapelW(y) ? 1 : 0;
  };
  const onWelt = (x, y) => (x > 0.22 && x < 0.4 && y < -0.965 && y > -1.0 ? 1 : 0);
  const onButton = (x, y) => (((x / 0.028) ** 2 + ((y + 1.035) / 0.028) ** 2) < 1 ? 1 : 0);
  // relief: the cloth stands where the lapel and tie are, and falls away into the V
  function suitRelief(x, y) {
    let d = 0.009 * onLapel(x, y) + 0.014 * onTie(x, y) + 0.006 * onCollar(x, y) - 0.005 * onShirt(x, y)
      + 0.01 * onDrape(x, y) + 0.005 * onShirtButton(x, y);
    d -= 0.004 * ss2(0.0, 0.02, Math.abs(Math.abs(x) - vEdge(y)));  // a crease where the fronts fold back
    return d;
  }

  const body = new THREE.Group(); person.add(body);
  const neck = new THREE.Mesh(new THREE.CylinderGeometry(0.135, 0.165, 0.4, 24), skin);
  neck.position.set(0, -0.47, -0.02); body.add(neck);

  const torsoGeo = new THREE.SphereGeometry(1, 160, 112);
  const tp = torsoGeo.attributes.position;
  for (let i = 0; i < tp.count; i++) {
    const dx = tp.getX(i), dy = tp.getY(i), dz = tp.getZ(i);
    let x = dx * T.sx, y = dy * T.sy + T.y, z = dz * T.sz;
    // square the shoulders a little and let the chest fall straight below them
    const up = ss2(0.1, 0.75, dy);
    x *= 1 + 0.12 * up * (1 - Math.abs(dx));
    const d = suitRelief(x, y) * ss2(0.05, 0.45, dz);
    let nx = dx / T.sx, ny = dy / T.sy, nz = dz / T.sz; const nl = Math.hypot(nx, ny, nz);
    tp.setXYZ(i, x + (nx / nl) * d, y + (ny / nl) * d, z + (nz / nl) * d);
  }
  tp.needsUpdate = true; torsoGeo.computeVertexNormals();

  // the paint, in the same coordinates as the relief
  const TW = 1024, TH = 512, tcv = document.createElement("canvas"); tcv.width = TW; tcv.height = TH;
  const tctx = tcv.getContext("2d"), timg = tctx.createImageData(TW, TH), tpx = timg.data;
  const SUIT = womanSuit ? [96, 101, 110] : [40, 47, 61];                       // a light grey blazer, or the navy suit
  const FACING = womanSuit ? [112, 118, 128] : [58, 67, 86];
  const CREASE = womanSuit ? [58, 62, 69] : [20, 24, 33];
  const SHIRT = womanSuit ? [246, 248, 252] : [240, 244, 250];
  const SHADE2 = womanSuit ? [198, 204, 214] : [186, 198, 216];
  const BTN = [20, 24, 32];
  const TIE = rgb(accent), TIE_D = rgb(accent, 0.62);
  const tc = [0, 0, 0];
  const tmix = (to, al) => { if (al <= 0) return; al = Math.min(1, al); tc[0] += (to[0] - tc[0]) * al; tc[1] += (to[1] - tc[1]) * al; tc[2] += (to[2] - tc[2]) * al; };
  for (let r = 0; r < TH; r++) {
    const th = (r + 0.5) / TH * Math.PI, sth = Math.sin(th), cth = Math.cos(th);
    for (let col = 0; col < TW; col++) {
      const ph = (col + 0.5) / TW * Math.PI * 2;
      const dx = -Math.cos(ph) * sth, dy = cth, dz = Math.sin(ph) * sth;
      const up = ss2(0.1, 0.75, dy);
      const x = dx * T.sx * (1 + 0.12 * up * (1 - Math.abs(dx))), y = dy * T.sy + T.y;
      const front = ss2(0.02, 0.3, dz), ax = Math.abs(x);
      tc[0] = SUIT[0]; tc[1] = SUIT[1]; tc[2] = SUIT[2];
      tmix(FACING, 0.9 * front * onLapel(x, y));
      tmix(CREASE, 0.95 * front * (1 - ss2(0.0, 0.016, Math.abs(ax - vEdge(y)))) * (y < -0.44 && y > -1.12 ? 1 : 0));
      tmix(CREASE, 0.7 * front * (1 - ss2(0.0, 0.014, Math.abs(ax - (vEdge(y) + lapelW(y))))) * (y < -0.46 && y > -1.05 ? 1 : 0));
      tmix(CREASE, 0.3 * front * onLapel(x, y) * ss2(0.0, 0.05, Math.abs(ax - vEdge(y))) * (1 - ss2(0.05, 0.1, Math.abs(ax - vEdge(y)))));
      // the notch where the collar meets the lapel
      const ny2 = y + 0.58, nx2 = ax - 0.16;
      tmix(CREASE, 0.9 * front * (1 - ss2(0.0, 0.02, Math.hypot(nx2 * 1.4, ny2))) * (ax > vEdge(y) ? 1 : 0));
      tmix(SHIRT, front * onShirt(x, y));
      tmix(SHADE2, 0.35 * front * onShirt(x, y) * (1 - ss2(0.0, 0.03, Math.abs(ax - vEdge(y)))));  // the shirt in shadow at the fold
      tmix(SHIRT, front * onCollar(x, y));
      tmix(SHADE2, 0.7 * front * onCollar(x, y) * (1 - ss2(0.0, 0.014, Math.abs(ax - lerp(0.125, 0.075, ss2(-0.45, -0.6, y))))));
      tmix(SHADE2, 0.5 * front * onShirt(x, y) * (1 - ss2(-0.6, -0.64, y)) * ss2(-0.56, -0.6, y));
      tmix(SHADE2, 0.7 * front * onPlacket(x, y));                               // the shirt's placket seam
      tmix(SHADE2, front * onShirtButton(x, y));
      tmix(TIE, front * onTie(x, y));
      tmix(TIE, front * onDrape(x, y));
      tmix(TIE_D, 0.45 * front * onDrape(x, y) * (1 - ss2(0.0, 0.012, Math.abs(ax - lerp(0.15, 0.05, ss2(-0.46, -0.8, y))))));
      tmix(TIE_D, 0.5 * front * onTie(x, y) * (1 - ss2(0.0, 0.012, Math.abs(ax - tieHalf(y)))));
      tmix(TIE_D, 0.6 * front * (y < -0.565 && y > -0.585 && Math.abs(x) < 0.05 ? 1 : 0));  // under the knot
      tmix(CREASE, 0.8 * front * onWelt(x, y));
      if (!womanSuit) tmix(BTN, front * onButton(x, y));
      const o = (r * TW + col) * 4;
      tpx[o] = tc[0]; tpx[o + 1] = tc[1]; tpx[o + 2] = tc[2]; tpx[o + 3] = 255;
    }
  }
  tctx.putImageData(timg, 0, 0);
  const suitMap = new THREE.CanvasTexture(tcv); suitMap.colorSpace = THREE.SRGBColorSpace; suitMap.anisotropy = 4;
  const torso = new THREE.Mesh(torsoGeo, clay(0xffffff, { map: suitMap, roughness: 0.62, clearcoat: 0.22 }));
  body.add(torso);

  // ---- head: one big smooth egg, with everything else set onto its surface ----
  const HEAD = { r: 0.36, sx: 1, sy: 1.04, sz: 0.96 };
  const onHead = (x, y, out = 0) => HEAD.sz * Math.sqrt(Math.max(0, HEAD.r * HEAD.r - (x / HEAD.sx) ** 2 - (y / HEAD.sy) ** 2)) + out;
  const head = new THREE.Group(); person.add(head);
  const skull = new THREE.Mesh(new THREE.SphereGeometry(HEAD.r, 64, 48), skin);
  skull.scale.set(HEAD.sx, HEAD.sy, HEAD.sz); head.add(skull);
  const earL = new THREE.Mesh(new THREE.SphereGeometry(0.075, 20, 16), skin);
  earL.position.set(-0.365, -0.03, 0.0); earL.scale.set(0.55, 1, 0.75); head.add(earL);
  const earR = earL.clone(); earR.position.x = 0.365; head.add(earR);
  const earInL = new THREE.Mesh(new THREE.SphereGeometry(0.038, 14, 12), clay(0xe7b7a5));
  earInL.position.set(-0.378, -0.03, 0.02); earInL.scale.set(0.5, 1, 0.7); head.add(earInL);
  const earInR = earInL.clone(); earInR.position.x = 0.378; head.add(earInR);

  // hair: ONE sculpted volume. Overlapping blobs leave intersection seams and read as lumps, and open sphere segments
  // leave rims that look like shards, so the cut is shaped into a single closed surface instead: the sweep is a ridge
  // running across the head, the part is a groove cut beside it, strands are fine ripples along the flow, and below
  // the hairline the surface tucks inside the skull so its only visible edge is that hairline.
  const smoothstep = (a0, b0, t) => { const k = Math.min(1, Math.max(0, (t - a0) / (b0 - a0))); return k * k * (3 - 2 * k); };
  const bump = (x, y, cx, cy, sx, sy) => Math.exp(-((((x - cx) / sx) ** 2 + ((y - cy) / sy) ** 2)) / 2);
  // (u, v) along and across a direction: u runs with the hair, v across it, which is how a cut is actually shaped
  const flow = (x, y, cx, cy, ang) => {
    const dx0 = x - cx, dy0 = y - cy, c = Math.cos(ang), s2 = Math.sin(ang);
    return [dx0 * c + dy0 * s2, -dx0 * s2 + dy0 * c];
  };
  const ridge = (u, v, su, sv) => Math.exp(-(((u / su) ** 2 + (v / sv) ** 2)) / 2);
  // where the hair stops, in head units: high across the forehead, dipping at the temples, low at the back
  const longHair = gender === "female";  // hair worn long: over the shoulders, framing the face
  function hairEdge(x, z) {
    const ax = Math.abs(x);
    let front = 0.255 - 0.05 * smoothstep(0.05, 0.26, ax) - 0.14 * smoothstep(0.22, 0.42, ax);
    front -= 0.05 * smoothstep(-0.02, -0.3, x);    // the fringe sweeps lower on this side
    front += 0.03 * smoothstep(0.02, 0.26, x);     // and the part sits higher on the other
    if (longHair) {
      // the hairline stays across the forehead, but past the temples the hair keeps going: down over the ears and
      // below the jaw, which is what makes the silhouette read as long hair rather than a cut
      front = 0.245 - 0.02 * smoothstep(0.05, 0.22, ax) - 0.66 * smoothstep(0.19, 0.33, ax);
      front -= 0.04 * smoothstep(-0.02, -0.28, x);   // a softer sweep on the parted side
      return front * smoothstep(-0.08, 0.3, z) + -0.95 * (1 - smoothstep(-0.08, 0.3, z));
    }
    return front * smoothstep(-0.08, 0.3, z) + -0.3 * (1 - smoothstep(-0.08, 0.3, z));
  }
  // how far the cut stands off the skull at (x, y), before it is tucked in below the hairline
  function hairLift(x, y, dz) {
    const frontness = smoothstep(-0.1, 0.4, dz);
    // the sweep: a long ridge lying diagonally across the front of the head, thick at the part and tapering away
    const [su1, sv1] = flow(x, y, -0.05, 0.245, 0.42);
    const sweep = ridge(su1, sv1, 0.3, 0.1) * (1 - 0.35 * smoothstep(0.1, 0.34, su1));
    // the lock that carries on down to the temple, where the sweep ends
    const [su2, sv2] = flow(x, y, -0.245, 0.135, 1.1);
    const lock = ridge(su2, sv2, 0.14, 0.075);
    // the crown, so the back of the head is not flat
    const crown = bump(x, y, 0.05, 0.3, 0.34, 0.17);
    // the part: a groove cut on the other side of the sweep
    const [pu, pv] = flow(x, y, 0.15, 0.285, -0.75);
    const part = ridge(pu, pv, 0.17, 0.022);
    // strands: fine ripples along the direction the hair runs, so the surface is not a poured shell
    const strands = Math.sin(su1 * 26) * 0.5 + Math.sin(su1 * 41 + 1.7) * 0.3;
    // the sides stay tight to the head: thickness there is what turns a cut into a mushroom
    const tight = 1 - 0.75 * smoothstep(0.17, 0.34, Math.abs(x));
    if (longHair) {
      // fuller at the sides and through the length, with a soft fringe rather than a man's hard sweep
      const body = 0.05 * smoothstep(0.1, 0.36, Math.abs(x)) + 0.05 * (1 - smoothstep(-0.7, 0.1, y));
      const fringe = 0.028 * ridge(...flow(x, y, -0.02, 0.23, 0.12), 0.26, 0.09) * frontness;
      return 0.022 + 0.03 * crown + body + fringe + 0.004 * strands * (crown + 0.5) * frontness;
    }
    return ((0.009 + 0.02 * crown) * tight + 0.085 * sweep * frontness + 0.05 * lock * frontness
      - 0.026 * part * frontness + 0.005 * strands * (sweep + crown) * frontness);
  }
  const HR = { x: HEAD.r * 1.025, y: HEAD.r * 1.075, z: HEAD.r * 0.99 };
  const hairGeo = new THREE.SphereGeometry(1, 256, 160);
  const hpos = hairGeo.attributes.position;
  for (let i = 0; i < hpos.count; i++) {
    const dx = hpos.getX(i), dy = hpos.getY(i), dz = hpos.getZ(i);
    const x = dx * HR.x, y = dy * HR.y + 0.012, z = dz * HR.z;
    // below the hairline the surface tucks inside the skull, so its edge is the hairline and nothing else
    const hide = 1 - smoothstep(-0.05, 0.05, y - hairEdge(x, dz));
    // the volume has to stop at the hairline too: a bump that keeps inflating below it pushes the tucked-in surface
    // back out through the skin, and the fringe then droops over the forehead down to wherever the bump fades
    const r = (1 + (hairLift(x, y, dz) * (1 - hide)) / HEAD.r) * (1 - 0.25 * hide);
    hpos.setXYZ(i, x * r, y * r, z * r);
  }
  hpos.needsUpdate = true; hairGeo.computeVertexNormals();
  const hair = new THREE.Mesh(hairGeo, hairMat); hair.position.z = 0.012; head.add(hair);
  // sideburns: short tapers in front of the ears, where the cut would be clipped
  const burnL = new THREE.Mesh(new THREE.SphereGeometry(0.055, 18, 14), hairMat);
  burnL.position.set(-0.315, 0.055, 0.03); burnL.scale.set(0.45, 1.15, 0.9); head.add(burnL);
  const burnR = burnL.clone(); burnR.position.x = 0.315; head.add(burnR);

  // brows: thick short strokes, following the curve of the forehead
  function brow(x) {
    const m = new THREE.Mesh(new THREE.CapsuleGeometry(0.021, 0.1, 6, 12), featureMat);
    m.position.set(x, 0.085, onHead(x, 0.085, -0.006));
    m.rotation.set(0, x < 0 ? 0.42 : -0.42, Math.PI / 2 + (x < 0 ? 0.1 : -0.1));
    head.add(m); return m;
  }
  const browL = brow(-0.15), browR = brow(0.15);

  // eyes: glossy black dots with a catchlight, the way the icon draws them
  function eye(x) {
    const g = new THREE.Group(); g.position.set(x, -0.02, onHead(x, -0.02, -0.018));
    const ball = new THREE.Mesh(new THREE.SphereGeometry(0.046, 24, 20), featureMat); g.add(ball);
    const spark = new THREE.Mesh(new THREE.SphereGeometry(0.012, 10, 8), new THREE.MeshBasicMaterial({ color: 0xffffff }));
    spark.position.set(0.014, 0.016, 0.038); g.add(spark);
    head.add(g); return g;
  }
  const eL = eye(-0.13), eR = eye(0.13);
  const nose = new THREE.Mesh(new THREE.SphereGeometry(0.034, 20, 16), skin);
  nose.position.set(0.01, -0.115, onHead(0.01, -0.115, 0.004)); nose.scale.set(1, 0.85, 0.8); head.add(nose);
  function blush(x) {
    const m = new THREE.Mesh(new THREE.CircleGeometry(0.058, 24), blushMat);
    m.position.set(x, -0.1, onHead(x, -0.1, 0.003)); m.lookAt(x * 4, -0.1, 3); head.add(m); return m;
  }
  blush(-0.225); blush(0.225);

  // mouth: a smile line while quiet, and while speaking an opening framed by lips, with depth behind it.
  // The opening is a LENS, not an ellipse: real lips meet at points, so an elliptical ring reads as a rubber
  // doughnut stuck on the face. A lens also survives being scaled vertically - the corners stay pinned at the ends
  // while the curves open - so one piece of geometry covers everything from closed to wide open.
  const MOUTH_Y = -0.2;
  const lens = (halfW, up, lo) => {
    const sh = new THREE.Shape();
    sh.moveTo(-halfW, 0);
    sh.quadraticCurveTo(-halfW * 0.45, up, 0, up);
    sh.quadraticCurveTo(halfW * 0.45, up, halfW, 0);
    sh.quadraticCurveTo(halfW * 0.45, -lo, 0, -lo);
    sh.quadraticCurveTo(-halfW * 0.45, -lo, -halfW, 0);
    return sh;
  };
  const mouth = new THREE.Group(); mouth.position.set(0.01, MOUTH_Y, onHead(0.01, MOUTH_Y, -0.004)); head.add(mouth);
  // the cavity: the opening extruded backwards into the head, so an open mouth recedes instead of sitting flat
  const cavityGeo = new THREE.ExtrudeGeometry(lens(0.073, 0.042, 0.055), { depth: 0.11, bevelEnabled: false, curveSegments: 16 });
  cavityGeo.translate(0, 0, -0.115);
  const cavity = new THREE.Mesh(cavityGeo, mouthMat); mouth.add(cavity);
  const tongue = new THREE.Mesh(new THREE.SphereGeometry(0.05, 22, 16), tongueMat);
  tongue.scale.set(0.9, 0.28, 0.5); tongue.position.set(0, -0.022, -0.03); tongue.visible = false; mouth.add(tongue);
  // the resting smile: one curved line, no lip geometry around the opening
  const smile = new THREE.Group(); smile.scale.set(1, 0.5, 1); smile.position.y = 0.036; mouth.add(smile);
  const smileArc = new THREE.Mesh(new THREE.TorusGeometry(0.085, 0.0095, 10, 48, Math.PI * 0.66), smileMat);
  smileArc.rotation.z = Math.PI + Math.PI * 0.17; smile.add(smileArc);  // the arc centred on the bottom of the ring

  if (longHair) {
    // the length the skull itself cannot carry: two falls beside the face and a mass down the back, as closed shapes
    burnL.visible = burnR.visible = false;
    const fallL = new THREE.Mesh(new THREE.CapsuleGeometry(0.12, 0.34, 8, 26), hairMat);
    fallL.position.set(-0.3, -0.32, -0.02); fallL.scale.set(0.6, 1, 0.8); fallL.rotation.z = -0.08; head.add(fallL);
    const fallR = fallL.clone(); fallR.position.x = 0.3; fallR.rotation.z = 0.08; head.add(fallR);
    const backFall = new THREE.Mesh(new THREE.CapsuleGeometry(0.31, 0.4, 8, 30), hairMat);
    backFall.position.set(0, -0.3, -0.13); backFall.scale.set(1, 1, 0.5); head.add(backFall);
    const nape = new THREE.Mesh(new THREE.SphereGeometry(0.29, 28, 22), hairMat);
    nape.position.set(0, -0.1, -0.11); nape.scale.set(1.1, 1, 0.9); head.add(nape);
  }

  // audio in -> the viseme detector only. The call is played by the page's <audio> element; routing it through Web
  // Audio as well would double it, and on iOS a remote WebRTC track played through an AudioContext crackles.
  // The chain still has to END at the destination or the browser never pulls it and the mouth stops moving - so it
  // ends there through a gain of zero, which is heard by nobody and keeps the analysis running.
  const gain = audioCtx.createGain();
  const silent = audioCtx.createGain();
  silent.gain.value = 0;
  silent.connect(audioCtx.destination);
  gain.connect(silent);
  const values = {};  // viseme name -> 0..0.75, written by HeadAudio, read by the render loop
  let ha = null, raf = 0, last = performance.now(), blinkAt = performance.now() + 2500, blink = 0, state = "listening", openS = 0, lastOpen = 0, frames = 0;

  function frame(now) {
    raf = requestAnimationFrame(frame);
    frames++;
    const dt = Math.min(64, now - last); last = now;
    if (ha) ha.update(dt);
    let open = 0, wide = 0, round = 0, weight = 0;
    for (const [k, v] of Object.entries(values)) {
      const s = SHAPES[k]; if (!s || v <= 0.001) continue;
      open += s[0] * v; wide += s[1] * v; round += s[2] * v; weight += v;
    }
    if (weight > 0.001) { open /= weight; wide /= weight; round /= weight; }
    lastOpen = open;
    openS += (open - openS) * Math.min(1, dt / 45);  // a little smoothing keeps the toy mouth from flickering
    const speaking = weight > 0.02;
    // the lens opens vertically and narrows as the lips round, and its corners stay put through all of it
    const w = 1.0 + wide * 0.26 - round * 0.4, h = 0.1 + openS * 1.05;
    cavity.scale.set(w, h, 1);
    cavity.position.y = -openS * 0.028;
    // the tongue rises into view only when the mouth is properly open
    tongue.visible = openS > 0.26;
    tongue.scale.set(0.88 + wide * 0.12, 0.2 + openS * 0.2, 0.5);
    tongue.position.y = -0.016 - openS * 0.05;
    const quiet = Math.max(0, 1 - openS * 4);
    smile.scale.set(1 + wide * 0.2, 0.5 * quiet + 0.05, 1); smile.visible = quiet > 0.05;
    // idle life: a slow sway, breathing, and a blink now and then
    const t = now / 1000;
    head.rotation.y = Math.sin(t * 0.45) * 0.07 + (state === "thinking" ? 0.12 : 0);
    head.rotation.x = Math.sin(t * 0.32) * 0.03 + (state === "thinking" ? 0.05 : 0) - (speaking ? 0.015 : 0);
    head.rotation.z = Math.sin(t * 0.27) * 0.02 + (state === "thinking" ? -0.06 : 0);
    head.position.y = Math.sin(t * 0.9) * 0.008 + openS * -0.006;
    body.position.y = Math.sin(t * 0.9) * 0.005;
    person.rotation.z = Math.sin(t * 0.21) * 0.012;
    if (now > blinkAt) { blink = 1; blinkAt = now + 2400 + Math.random() * 3600; }
    blink = Math.max(0, blink - dt / 110);
    const squash = 1 - Math.sin(Math.min(1, blink) * Math.PI) * 0.9;
    eL.scale.y = squash; eR.scale.y = squash;
    const lift = state === "thinking" ? 0.025 : speaking ? openS * 0.012 : 0;
    browL.position.y = 0.085 + lift; browR.position.y = 0.085 + lift + (state === "thinking" ? 0.012 : 0);
    renderer.render(scene, camera);
  }

  function resize() {
    const w = container.clientWidth || 320, h = container.clientHeight || 320;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    // frame head and shoulders whatever the panel's shape: a narrow panel needs the camera further back
    camera.position.set(0, -0.3, 3.05 / Math.min(1, Math.max(0.5, camera.aspect)));
    camera.lookAt(0, -0.3, 0);
    camera.updateProjectionMatrix();
  }
  const ro = new ResizeObserver(resize); ro.observe(container); resize();
  raf = requestAnimationFrame(frame);

  return {
    kind: "procedural",
    audioCtx,
    values,          // the live viseme values, so a stuck mouth can be diagnosed from the console
    get analysing() { return !!ha; },
    get debug() { return { frames, openS: +openS.toFixed(3), lastOpen: +lastOpen.toFixed(3) }; },
    async init() {
      ha = await headAudioFor(audioCtx);
      ha.onvalue = (k, v) => { values[k] = v; };
      gain.connect(ha);
    },
    attachStream(stream) {
      audioCtx.createMediaStreamSource(stream).connect(gain);
    },
    setState(s) { state = s; },
    dispose() {
      cancelAnimationFrame(raf); ro.disconnect();
      try { ha && ha.stop(); } catch {}
      renderer.dispose(); renderer.domElement.remove();
      try { if (audioCtx.state !== "closed") audioCtx.close(); } catch {}
    },
  };
}

// ---------------- a rigged GLB, if one is configured ----------------
async function glbDriver(container, { url, gender }) {
  const { TalkingHead } = await import("talkinghead");
  const head = new TalkingHead(container, {
    ttsEndpoint: "N/A", lipsyncModules: [], cameraView: "upper", cameraRotateEnable: false,
    cameraPanEnable: false, cameraZoomEnable: false, modelFPS: 30, avatarMood: "neutral",
  });
  await head.showAvatar({ url, body: gender === "female" ? "F" : "M", avatarMood: "neutral" });
  let ha = null;
  return {
    kind: "glb",
    audioCtx: head.audioCtx,
    async init() {
      ha = await headAudioFor(head.audioCtx);
      // tap the analyser, not the speech gain, so the head's own output can be silenced without silencing the mouth
      head.audioAnalyzerNode.connect(ha);
      try { head.audioSpeechGainNode.gain.value = 0; } catch {}  // the page's <audio> element is what you hear
      ha.onvalue = (k, v) => { const mt = head.mtAvatar[k]; if (mt) Object.assign(mt, { newvalue: v, needsUpdate: true }); };
      head.opt.update = ha.update.bind(ha);  // the head's own render loop advances the visemes
    },
    attachStream(stream) {
      head.audioCtx.createMediaStreamSource(stream).connect(head.audioAnalyzerNode);
    },
    setState(s) { try { head.setMood(s === "thinking" ? "neutral" : "happy"); } catch {} },
    dispose() { try { head.stop(); } catch {} container.replaceChildren(); },
  };
}

/** The avatar for a call. Falls back to the built-in face whenever the GLB or its library cannot be loaded. */
export async function createAvatar(container, { url = "", gender = "male", accent = "#1f5fd6" } = {}) {
  container.replaceChildren();
  if (url) {
    try {
      const d = await glbDriver(container, { url, gender });
      await d.init();
      return d;
    } catch (e) {
      console.warn("avatar: GLB unavailable, drawing the built-in face instead:", e);
      container.replaceChildren();
    }
  }
  const d = proceduralDriver(container, { gender, accent });
  await d.init();
  return d;
}
