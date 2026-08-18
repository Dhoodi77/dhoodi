(function () {
  'use strict';

  /* Mobile nav */
  var navToggle = document.querySelector('[data-mobile-nav-toggle]');
  var mobileNav = document.getElementById('MobileNav');
  if (navToggle && mobileNav) {
    navToggle.addEventListener('click', function () {
      var open = navToggle.getAttribute('aria-expanded') === 'true';
      navToggle.setAttribute('aria-expanded', String(!open));
      mobileNav.hidden = open;
    });
  }

  /* Search toggle */
  var searchToggle = document.querySelector('[data-search-toggle]');
  var headerSearch = document.getElementById('HeaderSearch');
  if (searchToggle && headerSearch) {
    searchToggle.addEventListener('click', function () {
      var open = searchToggle.getAttribute('aria-expanded') === 'true';
      searchToggle.setAttribute('aria-expanded', String(!open));
      headerSearch.hidden = open;
      if (!open) headerSearch.querySelector('input').focus();
    });
  }

  /* Cart drawer open/close */
  var cartDrawer = document.getElementById('CartDrawer');
  var cartOverlay = document.querySelector('.cart-drawer__overlay');
  var cartToggle = document.querySelector('[data-cart-toggle]');

  function openCart() {
    if (!cartDrawer) return;
    cartDrawer.hidden = false;
    cartOverlay.hidden = false;
    cartToggle && cartToggle.setAttribute('aria-expanded', 'true');
  }
  function closeCart() {
    if (!cartDrawer) return;
    cartDrawer.hidden = true;
    cartOverlay.hidden = true;
    cartToggle && cartToggle.setAttribute('aria-expanded', 'false');
  }
  if (cartToggle) cartToggle.addEventListener('click', openCart);
  document.querySelectorAll('[data-cart-close]').forEach(function (el) {
    el.addEventListener('click', closeCart);
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') closeCart();
  });

  /* AJAX add to cart on any product form, then refresh the drawer + open it */
  document.body.addEventListener('submit', function (e) {
    var form = e.target.closest('form[action*="/cart/add"]');
    if (!form) return;
    e.preventDefault();
    var formData = new FormData(form);
    fetch('/cart/add.js', { method: 'POST', body: formData, headers: { Accept: 'application/json' } })
      .then(function (res) { return res.json(); })
      .then(function () { return fetch('/cart.js'); })
      .then(function (res) { return res.json(); })
      .then(function (cart) {
        document.querySelectorAll('[data-cart-count]').forEach(function (el) {
          el.textContent = cart.item_count;
        });
        // Re-render the cart drawer section via the Section Rendering API in a real build:
        // fetch('/?sections=cart-drawer').then(...).then(html => cartDrawer.innerHTML = ...)
        openCart();
      })
      .catch(function (err) { console.error('Add to cart failed', err); });
  });

  /* Cart drawer quantity +/- (posts a full cart update on change) */
  document.body.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-qty-increase],[data-qty-decrease]');
    if (!btn) return;
    var input = btn.parentElement.querySelector('input[type="number"]');
    var delta = btn.hasAttribute('data-qty-increase') ? 1 : -1;
    input.value = Math.max(0, parseInt(input.value, 10) + delta);
    input.form && input.form.requestSubmit();
  });

  /* Product gallery: swap main image on thumbnail click */
  document.querySelectorAll('[data-product-gallery]').forEach(function (gallery) {
    var mainImg = gallery.querySelector('#ProductMainMedia');
    gallery.querySelectorAll('.product__thumb').forEach(function (thumb) {
      thumb.addEventListener('click', function () {
        var thumbImg = thumb.querySelector('img');
        if (mainImg && thumbImg) {
          mainImg.src = thumbImg.src.replace(/width=\d+/, 'width=1200');
          mainImg.srcset = '';
        }
      });
    });
  });

  /* Variant selection: update hidden variant id + price from product JSON, no full reload */
  document.querySelectorAll('[data-product-json]').forEach(function (section) {
    var product;
    try { product = JSON.parse(section.getAttribute('data-product-json')); } catch (err) { return; }
    var inputs = section.querySelectorAll('[data-option-input]');
    var variantIdField = section.querySelector('[data-variant-id]');
    if (!inputs.length || !variantIdField) return;

    function currentOptions() {
      var selected = [];
      var groups = {};
      inputs.forEach(function (i) {
        if (i.checked) groups[i.name] = i.value;
      });
      Object.keys(groups).sort().forEach(function (k) { selected.push(groups[k]); });
      return selected;
    }

    function findVariant() {
      var opts = currentOptions();
      return product.variants.find(function (v) {
        return v.options.every(function (o, idx) { return o === opts[idx]; });
      });
    }

    inputs.forEach(function (input) {
      input.addEventListener('change', function () {
        var variant = findVariant();
        if (!variant) return;
        variantIdField.value = variant.id;
        var addBtn = section.querySelector('[data-add-to-cart]');
        if (addBtn) {
          addBtn.disabled = !variant.available;
          addBtn.textContent = variant.available ? 'Add to cart' : 'Sold out';
        }
      });
    });
  });

  /* Sticky mobile add-to-cart bar: show once the buy box scrolls out of view */
  var stickyBar = document.querySelector('[data-sticky-add-to-cart]');
  var buyBox = document.querySelector('.product__form');
  if (stickyBar && buyBox && 'IntersectionObserver' in window) {
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        stickyBar.classList.toggle('is-visible', !entry.isIntersecting);
        stickyBar.hidden = entry.isIntersecting;
      });
    }, { rootMargin: '0px 0px -60% 0px' });
    observer.observe(buyBox);
  }

  /* Collection filter toggle (mobile) */
  var filterToggle = document.querySelector('[data-filter-toggle]');
  var filters = document.querySelector('.collection__filters');
  if (filterToggle && filters) {
    filterToggle.addEventListener('click', function () {
      var open = filters.classList.toggle('is-open');
      filterToggle.setAttribute('aria-expanded', String(open));
    });
  }
})();
