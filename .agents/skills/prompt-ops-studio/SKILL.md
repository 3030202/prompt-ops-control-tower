---
name: prompt-ops-studio
description: >-
  Руководство и процедуры работы со студией публикаций (Publishing Studio),
  генерацией постов от первого лица, созданием SVG prompt-cards и публикацией в Telegram.
---

# Prompt Ops Publishing Studio Guide

Этот навык описывает процесс создания, модерации и публикации постов в Telegram-каналы через `publishing_studio.py`.

## 1. Режимы генерации постов (`POST_MODES`)
- `step_by_step`: Пошаговое руководство с практическими инструкциями.
- `roast`: Критический разбор с сарказмом и анализом слабых мест.
- `my_take`: Личное экспертное мнение и выводы.
- `artifact_from_source`: Извлечение готового промпта, пайплайна или чеклиста из источника.
- `full_editorial`: Полный редакторский материал с контекстом, кодом и выводами.

## 2. Процедура публикации
1. **Отбор кандидатов**: Запрос `/api/sources` или `/api/artifacts` для выбора качественных материалов.
2. **Создание черновика**:
   - `POST /api/post-drafts` с указанием `source_record_id`, `mode`, `style_id`, `artifact_type`.
3. **Генерация и превью карточки**:
   - Вызов `generate_prompt_card_svg(title, text, tags, mode)` для создания брендированной SVG-карточки 1200x630.
   - Превью через `/api/publishing/cards/{card_hash}`.
4. **Утверждение и публикация**:
   - `POST /api/post-drafts/{id}/approve`
   - `POST /api/post-drafts/{id}/publish` (отправка в боевой канал с `photo` или `document`).
