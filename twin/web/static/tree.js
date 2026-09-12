/*
 * Procedural cherry blossom tree.
 *
 * Ported from the React original to vanilla SVG, with one substantive change:
 * the original always renders a finished tree, but here the tree has to *grow*
 * across the seven stages as commitments are kept.
 *
 * The growth is done by building the full tree once and then revealing it by
 * depth, rather than rebuilding per stage. That matters because the PRNG is
 * consumed in recursion order: rebuilding with a shallower depth would change
 * the random sequence and the trunk would visibly jump between stages. Built
 * once, the lower structure is identical at every stage and the tree simply
 * extends outward.
 */

const MAX_DEPTH = 8;
const GROW_TIME = 1.15;

// Blossoms attach to every branch from this depth outward, but only the
// current outermost band is displayed -- so a young tree carries a few buds at
// its tips rather than none at all. Kept at 1 so that stage 0, "First bud",
// actually has one.
const BLOSSOM_FROM_DEPTH = 1;
const DRIFTER_COUNT = 22;

const SVG_NS = "http://www.w3.org/2000/svg";

/* Deterministic PRNG, so the user's tree is the same tree every morning. */
function makeRandom(seed) {
  let s = seed >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 4294967296;
  };
}

function buildTree(seed) {
  const rand = makeRandom(seed);
  const branches = [];

  const grow = (x, y, angle, length, width, depth, startTime) => {
    // Curve each segment slightly so limbs read as organic, not geometric.
    const bend = (rand() - 0.5) * (depth < 2 ? 0.9 : 0.42);
    const midAngle = angle + bend * 0.5;
    const cx = x + Math.cos(midAngle) * length * 0.55;
    const cy = y + Math.sin(midAngle) * length * 0.55;
    const ex = x + Math.cos(angle + bend) * length;
    const ey = y + Math.sin(angle + bend) * length;

    const duration = GROW_TIME * (0.45 + length / 150);
    const endTime = startTime + duration;

    const blossoms = [];
    if (depth >= BLOSSOM_FROM_DEPTH) {
      const count = 2 + Math.floor(rand() * 2);
      for (let i = 0; i < count; i++) {
        blossoms.push({
          x: ex + (rand() - 0.5) * 34,
          y: ey + (rand() - 0.5) * 30,
          r: 4.6 + rand() * 3.4,
          rot: rand() * 360,
          delay: endTime + rand() * 1.2,
          tone: Math.floor(rand() * 3),
        });
      }
    }

    branches.push({
      d:
        "M " + x.toFixed(2) + " " + y.toFixed(2) +
        " Q " + cx.toFixed(2) + " " + cy.toFixed(2) +
        " " + ex.toFixed(2) + " " + ey.toFixed(2),
      width,
      depth,
      delay: startTime,
      duration,
      blossoms,
    });

    if (depth >= MAX_DEPTH || length < 9) return;

    // The lower trunk splits into main limbs; the upper structure forks more.
    const forks = depth < 2 ? 2 : rand() < 0.28 ? 3 : 2;
    const spread = depth < 2 ? 0.34 + rand() * 0.12 : 0.6 + rand() * 0.38;

    for (let i = 0; i < forks; i++) {
      const offset = (i / (forks - 1) - 0.5) * 2 * spread + (rand() - 0.5) * 0.22;
      grow(
        ex,
        ey,
        angle + bend + offset,
        length * (0.68 + rand() * 0.16),
        Math.max(0.55, width * (0.62 + rand() * 0.12)),
        depth + 1,
        endTime - duration * 0.08
      );
    }
  };

  grow(0, 0, -Math.PI / 2 + 0.04, 180, 32, 0, 0);
  return branches;
}

/* How much tree each stage reveals. Stage 0 is a sapling, stage 6 the full
 * canopy; the names in the UI ("First bud" … "Ancient sakura") track this. */
function depthForStage(stage, stageCount) {
  const span = MAX_DEPTH - 2;
  const ratio = stageCount > 1 ? stage / (stageCount - 1) : 1;
  return Math.round(2 + span * ratio);
}

function element(name, attributes) {
  const node = document.createElementNS(SVG_NS, name);
  for (const key in attributes) node.setAttribute(key, attributes[key]);
  return node;
}

function blossomNode(blossom, scale) {
  const radius = blossom.r * scale;
  const outer = element("g", {
    transform:
      "translate(" + blossom.x.toFixed(2) + " " + blossom.y.toFixed(2) +
      ") rotate(" + blossom.rot.toFixed(1) + ")",
  });
  // The inner group owns the bloom animation so its transform never clobbers
  // the placement transform on the outer one.
  const inner = element("g", { class: "cbt-blossom" });
  inner.style.animationDelay = blossom.delay.toFixed(2) + "s";

  [0, 72, 144, 216, 288].forEach((angle) => {
    inner.appendChild(
      element("ellipse", {
        cx: 0,
        cy: (-radius * 0.62).toFixed(2),
        rx: (radius * 0.44).toFixed(2),
        ry: (radius * 0.66).toFixed(2),
        transform: "rotate(" + angle + ")",
        class: "cbt-petal cbt-petal-" + blossom.tone,
      })
    );
  });
  inner.appendChild(element("circle", { r: (radius * 0.2).toFixed(2), class: "cbt-stamen" }));

  outer.appendChild(inner);
  return outer;
}

let cachedTree = null;

/**
 * Render the tree into `container` at the given growth stage.
 */
export function renderCherryBlossom(container, stage, stageCount, seed) {
  if (!cachedTree) cachedTree = buildTree(seed || 20260912);

  const maxDepth = depthForStage(stage, stageCount);
  // Buds on a young tree are smaller than blossoms on an old one.
  const blossomScale = 0.55 + 0.45 * (stageCount > 1 ? stage / (stageCount - 1) : 1);

  const svg = element("svg", {
    class: "cbt-canvas",
    viewBox: "-520 -1100 1040 1120",
    preserveAspectRatio: "xMidYMax meet",
    "aria-hidden": "true",
  });

  const defs = element("defs", {});
  defs.innerHTML =
    '<linearGradient id="cbt-bark" x1="0" y1="1" x2="0.35" y2="0">' +
    '<stop offset="0%" stop-color="var(--bark-root)"/>' +
    '<stop offset="45%" stop-color="var(--bark-mid)"/>' +
    '<stop offset="100%" stop-color="var(--bark-tip)"/></linearGradient>' +
    '<radialGradient id="cbt-canopy" cx="50%" cy="42%" r="52%">' +
    '<stop offset="0%" stop-color="var(--canopy-glow)" stop-opacity="0.75"/>' +
    '<stop offset="100%" stop-color="var(--canopy-glow)" stop-opacity="0"/></radialGradient>' +
    '<filter id="cbt-soft" x="-40%" y="-40%" width="180%" height="180%">' +
    '<feGaussianBlur stdDeviation="7"/></filter>';
  svg.appendChild(defs);

  svg.appendChild(
    element("ellipse", {
      cx: 0, cy: 4, rx: 230, ry: 24, class: "cbt-ground", filter: "url(#cbt-soft)",
    })
  );

  const sway = element("g", { class: "cbt-sway" });
  const scaled = element("g", { transform: "scale(1.35)" });

  // The canopy glow only makes sense once there's a canopy.
  if (stage >= Math.floor(stageCount / 2)) {
    const glow = element("ellipse", {
      cx: 0, cy: -470, rx: 360, ry: 250,
      fill: "url(#cbt-canopy)", class: "cbt-canopy-glow",
    });
    scaled.appendChild(glow);
  }

  const visible = cachedTree.filter((branch) => branch.depth < maxDepth);
  visible.forEach((branch) => {
    const path = element("path", {
      class: "cbt-branch",
      d: branch.d,
      pathLength: 1,
      stroke: "url(#cbt-bark)",
      "stroke-width": branch.width.toFixed(2),
      "stroke-linecap": "round",
      fill: "none",
    });
    path.style.animationDelay = branch.delay.toFixed(2) + "s";
    path.style.animationDuration = branch.duration.toFixed(2) + "s";
    scaled.appendChild(path);
  });

  // Only the outermost band blossoms, so the canopy sits at the tips rather
  // than flowering all the way down the trunk.
  visible
    .filter((branch) => branch.depth === maxDepth - 1)
    .forEach((branch) => {
      branch.blossoms.forEach((blossom) => {
        scaled.appendChild(blossomNode(blossom, blossomScale));
      });
    });

  sway.appendChild(scaled);
  svg.appendChild(sway);

  container.innerHTML = "";
  container.appendChild(svg);
  container.appendChild(buildDrifters(seed || 20260912, stage, stageCount));
}

function buildDrifters(seed, stage, stageCount) {
  const rand = makeRandom(seed + 991);
  const fall = document.createElement("div");
  fall.className = "cbt-fall";
  fall.setAttribute("aria-hidden", "true");

  // A bare tree shouldn't be shedding petals.
  const ratio = stageCount > 1 ? stage / (stageCount - 1) : 1;
  const count = Math.round(DRIFTER_COUNT * ratio);

  for (let i = 0; i < count; i++) {
    const petal = document.createElement("span");
    petal.className = "cbt-drifter cbt-petal-" + Math.floor(rand() * 3) + "-bg";
    petal.style.left = (rand() * 100).toFixed(1) + "%";
    const size = 5 + rand() * 5;
    petal.style.width = size.toFixed(1) + "px";
    petal.style.height = (size * 0.82).toFixed(1) + "px";
    petal.style.animationDelay = (rand() * 18).toFixed(2) + "s";
    petal.style.animationDuration = (13 + rand() * 13).toFixed(2) + "s";
    petal.style.setProperty("--cbt-drift", ((rand() - 0.5) * 220).toFixed(0) + "px");
    petal.style.setProperty("--cbt-spin", (240 + rand() * 500).toFixed(0) + "deg");
    fall.appendChild(petal);
  }
  return fall;
}
