document.addEventListener("DOMContentLoaded", () => {
  const root = document.documentElement;
  const themeToggle = document.getElementById("theme-toggle");
  if (themeToggle) {
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
    let switchingTheme = false;
    let fadeTimer;

    const applyTheme = (theme) => {
      root.dataset.theme = theme;
      const dark = theme === "dark";
      const label = dark ? "Включить светлую тему" : "Включить тёмную тему";
      themeToggle.setAttribute("aria-label", label);
      themeToggle.setAttribute("aria-pressed", String(dark));
      themeToggle.title = label;
      document.querySelector('meta[name="theme-color"]').content = dark ? "#151816" : "#f4f1e9";
      try { localStorage.setItem("atlas-theme", theme); } catch {}
    };
    applyTheme(root.dataset.theme === "dark" ? "dark" : "light");

    themeToggle.addEventListener("click", async () => {
      if (switchingTheme) return;
      const theme = root.dataset.theme === "dark" ? "light" : "dark";

      if (reducedMotion.matches || typeof document.startViewTransition !== "function") {
        if (!reducedMotion.matches) {
          clearTimeout(fadeTimer);
          root.classList.add("theme-fade");
          fadeTimer = setTimeout(() => root.classList.remove("theme-fade"), 500);
        }
        applyTheme(theme);
        return;
      }

      const rect = themeToggle.getBoundingClientRect();
      const x = rect.left + rect.width / 2;
      const y = rect.top + rect.height / 2;
      const radius = Math.hypot(Math.max(x, window.innerWidth - x), Math.max(y, window.innerHeight - y));
      switchingTheme = true;
      themeToggle.setAttribute("aria-disabled", "true");
      root.classList.add("theme-reveal");
      let transition;
      try {
        transition = document.startViewTransition(() => applyTheme(theme));
        await transition.ready;
        const reveal = root.animate(
          { clipPath: [`circle(0px at ${x}px ${y}px)`, `circle(${Math.ceil(radius)}px at ${x}px ${y}px)`] },
          { duration: 650, easing: "cubic-bezier(.2, .8, .2, 1)", pseudoElement: "::view-transition-new(root)" }
        );
        await Promise.all([reveal.finished, transition.finished]);
      } catch {
        transition?.skipTransition();
        applyTheme(theme);
      } finally {
        root.classList.remove("theme-reveal");
        themeToggle.removeAttribute("aria-disabled");
        switchingTheme = false;
      }
    });
  }

  const tabs = [...document.querySelectorAll(".tab-btn")];
  const products = [...document.querySelectorAll(".product-row")];

  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      const filter = tab.dataset.filter;
      tabs.forEach((item) => {
        const selected = item === tab;
        item.classList.toggle("active", selected);
        item.setAttribute("aria-pressed", String(selected));
      });
      products.forEach((product) => {
        product.hidden = filter !== "all" && product.dataset.category !== filter;
      });
    });
  });

  const faqItems = [...document.querySelectorAll(".faq-item")];
  faqItems.forEach((item) => {
    const button = item.querySelector(".faq-question");
    const answer = item.querySelector(".faq-answer");
    button.addEventListener("click", () => {
      const open = button.getAttribute("aria-expanded") !== "true";
      faqItems.forEach((other) => {
        const otherButton = other.querySelector(".faq-question");
        const otherAnswer = other.querySelector(".faq-answer");
        other.classList.remove("active");
        otherButton.setAttribute("aria-expanded", "false");
        otherAnswer.style.maxHeight = null;
      });
      if (open) {
        item.classList.add("active");
        button.setAttribute("aria-expanded", "true");
        answer.style.maxHeight = `${answer.scrollHeight}px`;
      }
    });
  });
});
