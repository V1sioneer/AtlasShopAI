document.addEventListener("DOMContentLoaded", () => {
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
