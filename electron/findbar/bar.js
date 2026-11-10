/* The find bar's page. find.js runs the search in the window's page and
 * reports the count back. This page only holds the text and the keys. */
const bridge = window.findBar;
const form = document.getElementById("form");
const query = document.getElementById("query");
const count = document.getElementById("count");

// Typing starts a new search, Enter and the arrows move through its matches.
function search(again, forward = true) {
  bridge.search(query.value, forward, again);
}

function showCount(result) {
  const none = Boolean(result) && result.matches === 0;
  count.textContent = !result || !query.value ? ""
    : none ? "No results"
    : `${result.active} of ${result.matches}`;
  count.classList.toggle("is-none", none);
}

query.addEventListener("input", () => {
  if (!query.value) showCount(null);
  search(false);
});

query.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    e.preventDefault();
    bridge.close();
  } else if (e.key === "Enter" && !e.isComposing) {
    e.preventDefault();
    search(true, !e.shiftKey);
  }
});

// Escape closes the bar from its buttons too.
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && e.target !== query) bridge.close();
});

form.addEventListener("submit", (e) => e.preventDefault());
document.getElementById("prev").addEventListener("click", () => search(true, false));
document.getElementById("next").addEventListener("click", () => search(true, true));
document.getElementById("close").addEventListener("click", () => bridge.close());

bridge.onShow(({ search: again } = {}) => {
  query.focus();
  query.select();
  if (again && query.value) search(false);
});
bridge.onStep(({ forward } = {}) => {
  if (query.value) search(true, forward !== false);
  else query.focus();
});
bridge.onResult(showCount);
