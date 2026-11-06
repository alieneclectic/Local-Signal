'use strict';

const themeKey = 'local-signal-theme';

function applyTheme(theme) {
 const dark = theme === 'dark';
 document.documentElement.dataset.theme = dark ? 'dark' : 'light';
 document.querySelector('meta[name="theme-color"]').content = dark ? '#17221b' : '#244b3e';
}

applyTheme(localStorage.getItem(themeKey));
