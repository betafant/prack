// Top-view aircraft silhouettes (nose up), drawn once into a mask atlas for deck.gl.

const S = 64;

function paraglider(ctx) {
  ctx.beginPath(); // canopy: a curved wing, convex side forward
  ctx.moveTo(5, 31);
  ctx.quadraticCurveTo(32, 1, 59, 31);
  ctx.lineTo(55, 32);
  ctx.quadraticCurveTo(32, 14, 9, 32);
  ctx.closePath();
  ctx.fill();
  ctx.lineWidth = 2.2;
  ctx.beginPath(); // lines from the trailing edge to the pilot
  for (const [x, y] of [[10, 31], [21, 24], [43, 24], [54, 31]]) { ctx.moveTo(x, y); ctx.lineTo(32, 47); }
  ctx.stroke();
  ctx.beginPath();
  ctx.arc(32, 49, 6, 0, Math.PI * 2);
  ctx.fill();
}

function hangglider(ctx) {
  ctx.beginPath();
  ctx.moveTo(32, 5);
  ctx.lineTo(61, 47);
  ctx.quadraticCurveTo(47, 38, 32, 43);
  ctx.quadraticCurveTo(17, 38, 3, 47);
  ctx.closePath();
  ctx.fill();
  ctx.fillRect(30, 12, 4, 44); // keel + pilot
  ctx.beginPath();
  ctx.arc(32, 52, 4.5, 0, Math.PI * 2);
  ctx.fill();
}

function glider(ctx) {
  ctx.beginPath(); // long slender wing
  ctx.moveTo(1, 25); ctx.lineTo(30, 21); ctx.lineTo(34, 21); ctx.lineTo(63, 25);
  ctx.lineTo(63, 28); ctx.lineTo(34, 27); ctx.lineTo(30, 27); ctx.lineTo(1, 28);
  ctx.closePath();
  ctx.fill();
  ctx.beginPath(); // fuselage
  ctx.moveTo(32, 5); ctx.quadraticCurveTo(36, 8, 35, 20); ctx.lineTo(33.5, 56); ctx.lineTo(30.5, 56); ctx.lineTo(29, 20);
  ctx.quadraticCurveTo(28, 8, 32, 5);
  ctx.fill();
  ctx.fillRect(21, 53, 22, 4); // tailplane
}

function other(ctx) {
  ctx.beginPath();
  ctx.moveTo(32, 3); ctx.quadraticCurveTo(37, 6, 36, 18); ctx.lineTo(60, 28); ctx.lineTo(60, 33); ctx.lineTo(36, 30);
  ctx.lineTo(35, 48); ctx.lineTo(45, 55); ctx.lineTo(45, 59); ctx.lineTo(32, 56); ctx.lineTo(19, 59); ctx.lineTo(19, 55);
  ctx.lineTo(29, 48); ctx.lineTo(28, 30); ctx.lineTo(4, 33); ctx.lineTo(4, 28); ctx.lineTo(28, 18); ctx.quadraticCurveTo(27, 6, 32, 3);
  ctx.fill();
}

function arrow(ctx) {
  ctx.beginPath();
  ctx.moveTo(32, 3); ctx.lineTo(48, 26); ctx.lineTo(36, 22); ctx.lineTo(36, 60); ctx.lineTo(28, 60); ctx.lineTo(28, 22); ctx.lineTo(16, 26);
  ctx.closePath();
  ctx.fill();
}

const DRAW = { pg: paraglider, hg: hangglider, gl: glider, ot: other, arrow };

function build() {
  const names = Object.keys(DRAW);
  const canvas = document.createElement('canvas');
  canvas.width = S * names.length;
  canvas.height = S;
  const ctx = canvas.getContext('2d');
  const mapping = {};
  names.forEach((name, i) => {
    ctx.save();
    ctx.translate(i * S, 0);
    ctx.fillStyle = '#fff';
    ctx.strokeStyle = '#fff';
    ctx.lineCap = 'round';
    DRAW[name](ctx);
    ctx.restore();
    mapping[name] = { x: i * S, y: 0, width: S, height: S, mask: true };
  });
  return { url: canvas.toDataURL('image/png'), mapping };
}

let cached = null;
export function atlas() {
  if (!cached) cached = build();
  return cached;
}
