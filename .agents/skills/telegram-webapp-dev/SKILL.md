---
name: telegram-webapp-dev
description: >-
  Руководство по разработке, верстке и интеграции Telegram Mini Apps (WebApp)
  с поддержкой Telegram WebApp JavaScript SDK, мобильной адаптивности и тем оформления.
---

# Telegram WebApp Development Guide

## 1. Подключение SDK
В секции `<head>` подключается скрипт:
```html
<script src="https://telegram.org/js/telegram-web-app.js"></script>
```

## 2. Инициализация и управление жизненным циклом
```javascript
const tg = window.Telegram?.WebApp;
if (tg) {
  tg.ready();
  tg.expand(); // Развернуть на весь экран
  // Настройка цветов заголовка и фона под тему
  if (tg.setHeaderColor) tg.setHeaderColor('#090c10');
  if (tg.setBackgroundColor) tg.setBackgroundColor('#090c10');
}
```

## 3. Тактильный отклик (Haptic Feedback)
При нажатии на интерактивные элементы и карточки:
```javascript
if (tg?.HapticFeedback) {
  tg.HapticFeedback.impactOccurred('medium'); // 'light' | 'medium' | 'heavy' | 'rigid' | 'soft'
}
```

## 4. Стили и Safe Area Insets
Для предотвращения перекрытия контента элементами управления Telegram:
```css
padding-top: max(12px, env(safe-area-inset-top, 0px));
padding-bottom: calc(75px + env(safe-area-inset-bottom, 0px));
```
