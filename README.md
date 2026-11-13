# yandex-wordstat-mcp

[![CI](https://github.com/sergeiteterin/yandex-wordstat-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/sergeiteterin/yandex-wordstat-mcp/actions/workflows/ci.yml)

MCP-сервер для статистики [Яндекс Вордстата](https://wordstat.yandex.ru). Через него Claude, Cursor и другие MCP-клиенты получают частотность запросов, динамику по времени и разбивку по регионам. Данные берутся из официального [Search API в Yandex Cloud](https://yandex.cloud/ru/docs/search-api/).

*English: an MCP server for Yandex Wordstat keyword statistics (search volume, trends, regional split) via the official Yandex Cloud Search API. Setup is below; env vars and tool names are the same in any language.*

## Что умеет

| Инструмент | Что возвращает |
|---|---|
| `top_requests` | Сколько раз искали фразу за последние 30 дней, популярные запросы с этой фразой и похожие запросы |
| `dynamics` | Изменение частотности по месяцам, неделям или дням и долю фразы среди всех запросов |
| `regions_distribution` | Регионы или города, где ищут фразу, с индексом интереса: больше 100 — ищут чаще среднего |
| `find_region` | ID региона по названию для фильтров |

Фильтры: регионы (по ID или по точному названию — `"Москва"`) и устройства (`desktop`, `phone`, `tablet`).

Во фразах работают операторы Вордстата: `"кавычки"` фиксируют набор слов, `!слово` — словоформу, `+слово` учитывает стоп-слова, `-слово` исключает слово.

Примеры запросов к агенту:

> Сколько ищут «купить квартиру» в Казани с телефонов?
>
> Покажи сезонность запроса «шины зимние» по месяцам за два года.
>
> В каких городах больше всего интересуются «ипотека для IT»?

## Что нужно

1. Аккаунт в [Yandex Cloud](https://console.yandex.cloud) с платёжным аккаунтом.
2. Сервисный аккаунт с ролью `search-api.editor` в вашем каталоге.
3. [API-ключ](https://yandex.cloud/ru/docs/iam/operations/authentication/manage-api-keys) для этого сервисного аккаунта, область действия `yc.search-api.execute`.
4. [ID каталога](https://yandex.cloud/ru/docs/resource-manager/operations/folder/get-id) — строка вида `b1g...`.
5. [uv](https://docs.astral.sh/uv/getting-started/installation/).

Запросы к Вордстату платные и тарифицируются как запросы к Search API. Рекомендуем задать бюджет или квоту в консоли.

## Установка

### Claude Code

```bash
claude mcp add -s user wordstat \
  -e YANDEX_SEARCH_API_KEY=ваш_ключ \
  -e YANDEX_FOLDER_ID=ваш_folder_id \
  -- uvx --from git+https://github.com/sergeiteterin/yandex-wordstat-mcp yandex-wordstat-mcp
```

Перезапустите Claude Code и проверьте подключение командой `/mcp`.

### Claude Desktop, Cursor и другие клиенты

```json
{
  "mcpServers": {
    "wordstat": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/sergeiteterin/yandex-wordstat-mcp", "yandex-wordstat-mcp"],
      "env": {
        "YANDEX_SEARCH_API_KEY": "ваш_ключ",
        "YANDEX_FOLDER_ID": "ваш_folder_id"
      }
    }
  }
}
```

### Переменные окружения

| Переменная | Обязательна | Описание |
|---|---|---|
| `YANDEX_SEARCH_API_KEY` | да* | API-ключ сервисного аккаунта |
| `YANDEX_IAM_TOKEN` | да* | IAM-токен вместо ключа. Живёт 12 часов, подходит для разовых запусков |
| `YANDEX_FOLDER_ID` | да | ID каталога |

\* Нужна одна из двух.

## Что важно знать

- **Данные отстают на несколько недель.** Если запросить слишком свежий период, API вернёт ошибку, и сервер подскажет сдвинуть даты.
- **Дневная статистика — только за последние 60 дней.** Помесячная и понедельная доступны за несколько лет.
- **Даты выравниваются сами.** Для недель — с понедельника по воскресенье, для месяцев — с 1-го по последнее число. API требует именно так.
- **`share_percent`** — доля фразы среди всех запросов в Яндексе, в процентах.
- **Ключ остаётся у вас.** Сервер работает локально и обращается только к `searchapi.api.cloud.yandex.net`. Ключ не пишется в логи и не попадает в сообщения об ошибках.

## Разработка

```bash
git clone https://github.com/sergeiteterin/yandex-wordstat-mcp
cd yandex-wordstat-mcp
uv sync
uv run pytest
```

Тесты не ходят в сеть: ответы API подменяются через [respx](https://lundberg.github.io/respx/).

Чтобы проверить сервер вручную, запустите [MCP Inspector](https://github.com/modelcontextprotocol/inspector):

```bash
YANDEX_SEARCH_API_KEY=... YANDEX_FOLDER_ID=... npx @modelcontextprotocol/inspector uv run yandex-wordstat-mcp
```

## Лицензия

MIT. Проект неофициальный и не связан с Яндексом.
