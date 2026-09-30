// AtlasShop Interactive App Scripts
document.addEventListener("DOMContentLoaded", () => {

  // Передаём источник перехода в Telegram для дальнейшей аналитики.
  document.querySelectorAll('a[href="https://t.me/AtlasShopAI_bot"]').forEach(link => {
    link.setAttribute("href", "https://t.me/AtlasShopAI_bot?start=landing");
  });

  // 1. Category Filter Tabs
  const tabButtons = document.querySelectorAll(".tab-btn");
  const cards = document.querySelectorAll(".bento-grid .glass-card");

  tabButtons.forEach(btn => {
    btn.addEventListener("click", () => {
      tabButtons.forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      tabButtons.forEach(b => b.setAttribute("aria-selected", String(b === btn)));

      const filter = btn.getAttribute("data-filter");

      cards.forEach(card => {
        const category = card.getAttribute("data-category");
        if (filter === "all" || category === filter) {
          card.style.display = "flex";
          card.style.opacity = "1";
          card.style.transform = "translateY(0)";
        } else {
          card.style.display = "none";
        }
      });
    });
  });

  // 2. FAQ Accordion
  const faqItems = document.querySelectorAll(".faq-item");
  faqItems.forEach(item => {
    const questionBtn = item.querySelector(".faq-question");
    const answer = item.querySelector(".faq-answer");

    questionBtn.addEventListener("click", () => {
      const isActive = item.classList.contains("active");

      // Close all others
      faqItems.forEach(other => {
        other.classList.remove("active");
        const otherQuestion = other.querySelector(".faq-question");
        if (otherQuestion) otherQuestion.setAttribute("aria-expanded", "false");
        const otherAnswer = other.querySelector(".faq-answer");
        if (otherAnswer) otherAnswer.style.maxHeight = null;
      });

      // Toggle current
      if (!isActive) {
        item.classList.add("active");
        questionBtn.setAttribute("aria-expanded", "true");
        answer.style.maxHeight = answer.scrollHeight + "px";
      } else {
        item.classList.remove("active");
        answer.style.maxHeight = null;
      }
    });
  });

  // 3. Subtle Caustic Mouse Glow on Glass Cards (Desktop only)
  if (window.matchMedia("(pointer: fine)").matches) {
    cards.forEach(card => {
      card.addEventListener("mousemove", e => {
        const rect = card.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const y = e.clientY - rect.top;
        card.style.setProperty("--mouse-x", `${x}px`);
        card.style.setProperty("--mouse-y", `${y}px`);
      });
    });
  }

});
