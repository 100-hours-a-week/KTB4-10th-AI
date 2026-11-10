const book = document.querySelector(".book");
const pages = [...book.querySelectorAll(".page")];
const pos = document.getElementById("pos");
const prev = document.getElementById("prev");
const next = document.getElementById("next");
let cur = 0;

// 쪽(520 x 746)이 화면에 통째로 들어가는 비율. 좌우 16px · 위 여유 24px · 아래 버튼 자리 80px 를 뺀다.
// 큰 화면에서는 키우지 않는다
const fit = () => {
  const z = Math.min(1, (innerWidth - 32) / 520, (innerHeight - 104) / 746);
  book.style.setProperty("--z", z.toFixed(4));
};
// 장소 사진 높이: 원래 325px. 글이 가장 긴 장소 쪽이 넘치면 그 쪽에 맞춰 모든 장소 사진을 같이 줄인다 (최소 220px).
// 쪽을 줄이기(fit) 전, 원래 크기에서 한 번만 잰다 — 글 높이는 사진 높이와 상관없다
const fitPhotos = () => {
  const placePages = [...document.querySelectorAll(".page.place")];
  const textHeights = placePages.map((page) => page.querySelector(".pad").offsetHeight);
  const tallestText = Math.max(0, ...textHeights);
  const room = 746 - tallestText;
  const photoHeight = Math.max(220, Math.min(325, room));
  book.style.setProperty("--photo-h", photoHeight + "px");
};
fitPhotos();
fit();
addEventListener("resize", fit);

const go = (i) => {
  const target = pages[Math.max(0, Math.min(pages.length - 1, i))];
  target.scrollIntoView({ behavior: "smooth", inline: "center", block: "nearest" });
};
const show = () => {
  pos.textContent = `${cur + 1} / ${pages.length}`;
  prev.disabled = cur === 0;
  next.disabled = cur === pages.length - 1;
};
// 화면 가운데에 가장 가까운 쪽이 지금 쪽이다
const centerOf = (p) => p.offsetLeft + p.offsetWidth / 2;
book.addEventListener("scroll", () => {
  const mid = book.scrollLeft + book.clientWidth / 2;
  const nearer = (best, p, k) =>
    Math.abs(centerOf(p) - mid) < Math.abs(centerOf(pages[best]) - mid) ? k : best;
  const i = pages.reduce(nearer, 0);
  if (i !== cur) { cur = i; show(); }
}, { passive: true });

prev.onclick = () => go(cur - 1);
next.onclick = () => go(cur + 1);
document.addEventListener("keydown", (e) => {
  if (e.key === "ArrowRight") go(cur + 1);
  if (e.key === "ArrowLeft") go(cur - 1);
});
show();
