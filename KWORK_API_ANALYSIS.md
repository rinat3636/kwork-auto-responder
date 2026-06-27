# Kwork APK — анализ API для бота ответов на заявки

**Приложение:** `ru.kwork.app` v4.2.0 (versionCode 606)
**Источник:** официальный XAPK с APKCombo, декомпиляция jadx 1.5.0 + apktool 2.9.3
**Цель анализа:** понять, как бот может (1) получать список заявок биржи, (2) отправлять отклик с текстом, суммой и сроком.

> ⚠️ **Главный вывод:** в мобильном приложении НЕТ нативного REST-эндпоинта для *создания* отклика.
> Списки/чтение идут через REST API (`api.kwork.ru`), а **сама отправка отклика происходит через веб-форму
> `https://kwork.ru/new_offer?project=<id>`, открываемую в WebView с авторизацией по WebAuthToken.**
> Бот должен либо автоматизировать эту веб-форму (headless-браузер), либо воспроизвести её POST-запрос.

---

## 1. Базовые URL

`cy/d.java` (ServerManager) строит хосты по языку:

| Назначение | Шаблон | Для `ru` |
|---|---|---|
| REST API | `https://api.<domain>/` | `https://api.kwork.ru/` |
| Веб-сайт (WebView формы) | `https://<domain>/` | `https://kwork.ru/` |

`domain` = `kwork.ru` (locale ru) / `kwork.com` (en) / `<lang>.kwork.com` (прочие).

---

## 2. Аутентификация

### 2.1 Вход — `POST /signIn` (api.kwork.ru), form-urlencoded
```
login=<email/phone>
password=<password>
recaptcha_pass_token=<если требуется recaptcha>
uad=<device id, см. ниже>
device=<инфо об устройстве>
Authorization: <header>
```
Ответ `SignInResponse` (extends `DataResponse<SignInData>`):
- `SignInData.token` — основной токен сессии
- `phoneMask`, `recapthcaPassToken`, `isRegistration`

После входа `token`, `uad`, `slrememberme` сохраняются в SharedPreferences (`nr/d.java`).

### 2.2 Параметры авторизации в каждом запросе
Почти все запросы — `POST` + `@FormUrlEncoded`, и передают:

| Параметр | Где | Откуда берётся |
|---|---|---|
| `token` | поле формы | из `SignInData.token` |
| `uad` | поле формы | SHA-1 от атрибутов устройства + `android_id` + timestamp (см. 2.3) |
| `slrememberme` | поле формы | из prefs `prefsrememberme`; также шлётся как Cookie `slrememberme=<...>` |
| `device` | поле формы | строка с инфо об устройстве |
| `Authorization` | HTTP header | заголовок авторизации |
| `Cookie` | HTTP header | набор cookie, в т.ч. `slrememberme=<...>` |

### 2.3 Генерация `uad` (`nr/d.java`, метод `i()`)
```
seed = "35"
     + (Build.BOARD.length()%10) + (Build.BRAND.length()%10)
     + (Build.CPU_ABI.length()%10) + (Build.DEVICE.length()%10)
     + (Build.DISPLAY.length()%10) + (Build.HOST.length()%10)
     + (Build.ID.length()%10) + (Build.FINGERPRINT.length()%10)
     + (Build.MANUFACTURER.length()%10) + (Build.MODEL.length()%10)
     + (Build.PRODUCT.length()%10) + (Build.TAGS.length()%10)
     + (Build.TYPE.length()%10) + (Build.USER.length()%10)
uad = hex( SHA-1( seed + android_id + currentTimeMillis ) )   // сохраняется в prefs "uad"
```
Т.е. `uad` генерируется один раз на устройство и далее переиспользуется.

---

## 3. Получение списка заявок биржи — `POST /projects` (api.kwork.ru)

Метод `getWorkerProjects` (`ExchangeService.java`), form-urlencoded:
```
token, uad, slrememberme, device          # авторизация
categories   = <строка id категорий>
attributes   = <строка атрибутов фильтра>
price_from   = <int|null>
price_to     = <int|null>
hiring_from  = <int|null>
offers       = <строка>
query        = <строка поиска>
page         = <int>
Header: Authorization, Cookie
```
Ответ: `ExchangeProjectsApi` (список `ExchangeProject`).

Дополнительно:
- `POST /getWantsCount` → `ProjectsCount` (число заявок по фильтру)
- `POST /exchangeInfo`, `POST /categories`, `POST /favoriteCategories` — справочники/фильтры
- `POST /projects` (вариант `getConnects`) — заявки по категориям с `price_to`

---

## 4. Просмотр своих откликов

| Метод | Эндпоинт | Назначение | Ответ |
|---|---|---|---|
| POST | `/offers` (`getMyOffers`) | список своих откликов, `page` | `OffersApi` |
| POST | `/offer` (`getOffer`) | один отклик по `id` | `DataResponse<Offer>` |
| POST | `/deleteOffer` (`deleteOffer`) | удалить отклик по `id` | `BooleanResponse` |

### Модель `Offer` (поля из локальной БД Room)
```
offerId, offerStatus, offerTitle, offerPrice, duration, offerDateCreate,
isActual, readStatus, wantId, orderId, kworkId, comment, userId, userName,
profilePicture, price, status, title, description, offers, timeLeft,
categoryBasePrice, parentCategoryId, categoryId, dateConfirm,
userProjectsCount, userActiveProjectsCount, userHiredPercent, isViewed,
alreadyWork, achievementsList, typeConnect, portfolioRequiredStatus,
portfolioRequiredCategory, attachments
```
Ключевые для бота: `offerPrice` (сумма), `duration` (срок), `comment`/`offerTitle` (текст), `wantId`/`kworkId` (к какой заявке).

---

## 5. ⭐ Создание отклика — через WebView, НЕ через REST

### 5.1 Сборка URL формы (`ux/r.java`)
```java
// k(projectId): страница создания отклика
e("new_offer?project=%d".format(projectId), false)
   →  https://kwork.ru/new_offer?project=<projectId>

// j(projectId): страница заявки
   →  https://kwork.ru/projects/<projectId>
```
Аналитическое событие подтверждает: `appmetrica_event_exchange_worker_make_offer_url_open`
= *"Exchange Worker make offer url open"*.

### 5.2 Авторизация WebView — `POST /getWebAuthToken` (api.kwork.ru)
```
url_to_redirect = https://kwork.ru/new_offer?project=<id>
uad, token, slrememberme, device
Header: Cookie, Authorization
```
Ответ `DataResponse<WebAuthToken>`, где:
```
WebAuthToken { token: String, expiresAt: Long, url: String }
```
`url` — уже авторизованный URL, который грузится в `InAppWebActivity`
(`features/webview/InAppWebActivity.java`, intent extra `KEY_URL_TO_LOAD`).

### 5.3 Итоговый поток создания отклика
```
1. /signIn                       → token (+ uad, slrememberme в prefs)
2. /projects (getWorkerProjects) → список заявок, берём project id
3. строим URL: https://kwork.ru/new_offer?project=<id>
4. /getWebAuthToken(url_to_redirect=этот URL) → WebAuthToken.url (авторизованный)
5. WebView грузит WebAuthToken.url → веб-форма отклика на kwork.ru
6. Пользователь (=бот) заполняет: ТЕКСТ + СУММА + СРОК и сабмитит форму
   → submit идёт POST'ом на сайт kwork.ru (НЕ на api.kwork.ru)
```

**Поля формы (текст / сумма / срок) задаются HTML-формой на `kwork.ru/new_offer`, а не мобильным API.**
Точные `name` полей и URL сабмита в APK не зашиты — их надо снять с самой веб-страницы.

---

## 6. Что это значит для бота (рекомендации)

Два рабочих варианта:

**A. Полностью через REST для чтения + headless-браузер для отправки (надёжнее).**
- Логин и список заявок — REST (`/signIn`, `/projects`).
- Отправку отклика — через авторизованный браузер (Playwright/Selenium) по `https://kwork.ru/new_offer?project=<id>`: заполнить текст/сумму/срок, нажать «Отправить».
- Куки авторизации можно получить либо обычным логином на сайте, либо через `getWebAuthToken`.

**B. Воспроизвести POST веб-формы напрямую (быстрее, но хрупко).**
- Открыть `kwork.ru/new_offer?project=<id>`, считать имена полей и action формы (CSRF-токен, скрытые поля).
- Слать тот же POST с авторизационными cookie. Требует реверса самой веб-страницы (вне APK).

В обоих случаях бот работает с **веб-частью kwork.ru**, а мобильный API нужен в основном для удобного получения списка заявок и фильтров.

---

## 7. Полный список сервисов (Retrofit интерфейсы)

`ru/kwork/app/data/remote/`: `ActorService` (~45 эндпоинтов, аккаунт/авторизация),
`ExchangeService` (биржа/заявки/отклики — раздел 3–4), `OrderService` (заказы; `/createAnswer`
= ответ на *отзыв*, не отклик), `InboxService`, `FileService` и др.
Всего обнаружено **191 уникальный путь**. Все вызовы — `POST` + `@FormUrlEncoded`.

> Аннотации Retrofit в декомпиляции: `@o` = POST, `@e` = FormUrlEncoded, `@c` = @Field, `@i` = @Header.

---

## 8. РЕАЛЬНЫЙ эндпоинт отправки отклика (захвачен из JS-бандла сайта)

Источник: `https://cdn-edge.kwork.ru/js/dist/new-offer_*.js` (Vue-компонент формы `new_offer`).

### Маршрутизация сабмита (из кода `submitOffer`)
```js
var r = "/sendmessage";
if (isOfferPage && hasOffer)      r = "/api/offer/editoffer";   // редактирование существующего отклика
else if (isOfferPage)             r = "/api/offer/createoffer"; // НОВЫЙ отклик на странице new_offer
axios.post(r, formData)
```

| Контекст | Метод/URL | Назначение |
|---|---|---|
| Страница `new_offer?project=<id>`, отклика ещё нет | `POST /api/offer/createoffer` | **создать отклик (то, что нужно боту)** |
| Та же страница, отклик уже есть | `POST /api/offer/editoffer` | изменить отклик |
| Модалка «индивидуальное предложение» в чате | `POST /sendmessage` | оффер сообщением |
| Автосохранение черновика (НЕ сабмит) | `POST /wants/create_offer_draft` | autosave (можно игнорировать) |
| Валидация текста на лету | `POST /api/validation/checktext` | антиспам-проверка |

### Payload `POST /api/offer/createoffer` (FormData, `setRequestDataOfferPage`)
Для обычного (не-kwork, не-поэтапного) отклика:

| Поле | Значение | Комментарий |
|---|---|---|
| `wantId` | id проекта/заявки | = `project` из URL |
| `offerType` | `custom` | (или `kwork`, если предлагается готовый кворк) |
| `description` | текст предложения | ≥150 символов, прогоняется через `emojiReplacements.preSubmitMessage` |
| `kwork_duration` | **число дней** | целое из `durations.basic` = `[1,2,3,4,5,6,7,10,14,21,30,60]` (т.е. 3 = «3 дня») |
| `kwork_price` | сумма | `totalCommissionPrice` — цена, которую платит покупатель |
| `kwork_name` | название оффера | краткий заголовок |

Для kwork-оффера вместо `kwork_name`: `kwork_id`, `kwork_count`/`kwork_package_type`, `gextras[]`, `extra_count<N>`, `customExtraName[]`… (`setRequestKworkOffer`).
Для поэтапной оплаты: `stages[<n>][title]`, `stages[<n>][payer_price]` (`setRequestDataStages`).

### CSRF / заголовки
- Глобально axios шлёт `X-Requested-With: XMLHttpRequest`.
- `csrftoken` доступен на странице как `window.csrftoken`. Для HTTP-replay безопаснее добавить `csrftoken` и в тело, и снять `window.csrftoken` со страницы.
- Нужны cookie авторизованной сессии (домен `kwork.ru`).

### Ответ
`axios.post(...).then(t => t.data)`: `{ status, code, response, errors }`. `status === "error"` → ошибка (`code` 308/310 — нужны чеки/квитанции, 309 — модалка). Успех — закрытие модалки / редирект, списывается 1 коннект.

> ⚠️ Это веб-эндпоинт `kwork.ru` (не `api.kwork.ru`). `kwork_duration` = **число дней** (целое из `durations.basic` = `[1,2,3,4,5,6,7,10,14,21,30,60]`), `wantId` = id заявки из URL.
>
> Данные живой формы (проект 3205927): `csrftoken` в `window.csrftoken`, `durations.basic` как выше, `customMinPrice=1200`, `customMaxPrice=18000`. Селекторы формы: описание `div.trumbowyg-editor.js-stopwords-check` (Trumbowyg contenteditable), цена `input#offer-custom-price` (type=tel), срок `.v-select.duration-select` (vue-select), кнопка `button.kw-button--green` («Предложить»).
