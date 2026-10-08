/**
 * What the terminal says for itself, in the language the terminal is set to.
 *
 * Two things called "language" meet here and only one of them lives in this
 * file. The other is the `lang` the model passes every tool — the language the
 * person just spoke — and that one still decides what a search is run in and
 * what a sidecar model writes back (`tools/_lang.py`). This file is the
 * chrome: the settings sheet, the starter chips, the card headings, the line
 * that says the microphone would not open. None of it is an answer to
 * anything, so none of it should change because somebody switched language
 * mid-conversation, which is what it used to do.
 *
 * Two locales, because the household has two. English is the fallback for
 * every key and for every tag that is neither — a Swedish summary under a
 * heading that reads "Places" is a smaller wrong than a translation framework
 * in a kitchen appliance with two users. A third costs one block below and the
 * two `html[lang=…]` rules in style.css; nothing else here is per-locale.
 *
 * The tag lives on `<html lang>`, which is where `t` reads it back from. One
 * attribute doing two jobs: it is what the browser itself reads to pick a CJK
 * font, and reading it from the document rather than passing it in keeps every
 * card renderer's signature alone.
 */

/** The locales with a block below. English is also the fallback. */
export const LOCALES = ["en", "ja"];

/**
 * Every string the terminal writes that is not somebody else's text.
 *
 * `ui_*` is chrome that lives in `index.html`, `app.js` and `location.js`;
 * everything else is a card label. One table rather than one per file, because
 * the failure mode of two is a locale that is complete in one of them.
 *
 * `{n}`, `{q}`, `{who}` and `{label}` are substituted by the caller with
 * `.replace`. No plural machinery: English needs two forms for one sentence in
 * this whole table (`ui_forgot_one` / `ui_forgot_many`) and Japanese needs
 * none, so the two forms are two keys.
 */
export const STRINGS = {
  en: {
    ui_language: "Language",
    ui_voice: "Voice",
    ui_voice_set: "Voice set to {voice}. Reconnecting.",
    ui_settings: "Settings",
    ui_gws_expired: "Google Workspace access has expired. Please open Settings to reconnect your account.",
    ui_status_connecting: "Connecting",
    ui_ready: "Ready",
    ui_offline: "Offline",
    ui_who: "Who",
    ui_microphone: "Microphone",
    ui_mic_default: "System default",
    ui_mic_n: "Microphone {n}",
    ui_mic_off: "Mic off",
    ui_listening: "Listening",
    ui_mic_failed: "Could not open the microphone",
    ui_wake_again: "Asleep — say “Hey Gemini” or tap the mic to wake up.",
    ui_speaker_volume: "Speaker volume",
    ui_speaker_volume_at: "Speaker volume {level}",
    ui_speaker_muted: "Speaker muted",
    ui_mute: "Mute",
    ui_unmute: "Unmute",
    ui_speaker_muted_line: "Speaker muted. It still hears you.",
    ui_speaker_on: "Speaker on.",
    ui_interrupt: "Interruption",
    ui_interrupt_allow: "Allow barge-in while speaking",
    ui_interrupt_note_on: "Barge-in enabled. Reconnecting.",
    ui_interrupt_note_off: "Barge-in disabled. Reconnecting.",
    ui_location: "Location",
    ui_where_unset: "Not set — the server's HOME_LOCATION is in use",
    ui_where_set: "Location set to {label}. Reconnecting.",
    ui_set_on_map: "Set on a map…",
    ui_no_map: "No map: MAPS_API_KEY is missing or the API would not load.",
    ui_clear: "Clear conversation",
    ui_forget: "Forget my history",
    ui_forget_armed: "Tap again to forget",
    ui_forget_failed: "Could not forget: {e}. Nothing was deleted.",
    ui_reboot: "Restart terminal",
    ui_reboot_armed: "Tap again to restart",
    ui_rebooting: "Restarting terminal…",
    ui_reboot_failed: "Could not restart: {e}.",
    ui_forgot_one: "Forgot {n} conversation.",
    ui_forgot_many: "Forgot {n} conversations.",
    ui_forgot_none: "There was nothing to forget.",
    ui_now_talking: "Now talking to {who}.",
    ui_search_address: "Search an address…",
    ui_search: "Search",
    ui_use_position: "Use current position",
    ui_drag_map: "Drag the map",
    ui_cancel: "Cancel",
    ui_set_location: "Set location",
    ui_no_address: "No such address",
    ui_geocode_failed: "Could not look that address up",
    ui_finding_you: "Finding you…",
    ui_no_position: "The browser would not say where it is",
    ui_starters: "Tap a chip or the mic to start, or try",
    ui_starters_basic: "Basic Capabilities",
    ui_starters_context: "Suggestions",
    // The starter chips: `ui_chip_*` is the label on the button, `ui_ask_*` is
    // the sentence it sends, and `*_of` is the form used once the room has
    // named a topic. Four keys per chip rather than one, because a short label
    // stopped being a question the agent can answer.
    ui_chip_weather: "Today's Weather",
    ui_ask_weather: "What's the weather like today?",
    ui_chip_weather_of: "Weather in {q}",
    ui_ask_weather_of: "What's the weather like in {q}?",
    ui_chip_places: "Find Places",
    ui_ask_places: "Find a good place to eat nearby",
    ui_chip_places_of: "Places in {q}",
    ui_ask_places_of: "Find good places in {q}",
    ui_chip_routes: "Routes",
    ui_ask_routes: "I want to look up directions",
    ui_chip_routes_of: "Route to {q}",
    ui_ask_routes_of: "How do I get to {q}?",
    ui_chip_web_search: "Web Search",
    ui_ask_web_search: "Search the web for latest tech news",
    ui_chip_web_search_of: "Web search for {q}",
    ui_ask_web_search_of: "Search the web for {q}",
    ui_chip_video: "Video",
    ui_ask_video: "I want to watch a video",
    ui_chip_video_of: "Video of {q}",
    ui_ask_video_of: "Show me a video of {q}",
    ui_chip_news: "News",
    ui_ask_news: "What's in the news today?",
    ui_chip_news_of: "News about {q}",
    ui_ask_news_of: "What's the latest news about {q}?",
    ui_chip_market: "Market",
    ui_ask_market: "How are the markets doing today?",
    ui_chip_schedule: "Today's Schedule",
    ui_ask_schedule: "What's on my schedule today?",
    ui_chip_mail: "Recent Emails",
    ui_ask_mail: "What are my recent emails?",
    ui_chip_mail_of: "Emails about {q}",
    ui_ask_mail_of: "Find my emails about {q}",
    ui_chip_docs: "Find Docs",
    ui_ask_docs: "Find a document in Drive",
    ui_chip_docs_of: "Docs about {q}",
    ui_ask_docs_of: "Find my documents about {q}",
    ui_voice_mode: "Voice mode",
    ui_type_instead: "Type instead…",
    ui_send: "Send",
    ui_working: "Looking that up…",
    ui_no_answer: "No answer came back.",
    ui_lang_note: "The panel is in English. Reconnecting; map labels follow on the next reload.",
    source: "Source",
    head_web_search: "Web search",
    head_image_search: "Images",
    head_youtube_search: "Videos",
    head_maps_search: "Places",
    head_place_info: "Place",
    head_info: "Info",
    head_camera: "Camera",
    camera_auth_required: "Camera re-authentication required",
    head_home_summary: "Home status",
    head_home_timeline: "Activity timeline",
    near_around: "near {where}",
    head_gws: "Workspace",
    head_browse: "Browsing",
    browse_step: "Step {n}",
    browse_failed: "Could not read that page.",
    browse_lost: "The location changed before that finished.",
    call_browse: "opening a browser",
    on_the_way: "{q} on the way",
    mode_DRIVE: "Driving",
    mode_WALK: "Walking",
    mode_BICYCLE: "Cycling",
    mode_TRANSIT: "Transit",
    call_place_info: "looking up",
    call_web_search: "searching",
    call_image_search: "finding photos",
    call_youtube_search: "finding videos",
    call_maps_search: "searching the map",
    call_maps_route: "working out the route",
    call_maps_search_along_route: "looking along the route",
    call_gws: "asking Workspace",
    gws_calendar: "Reading the calendar",
    gws_mail: "Searching mail",
    gws_drive: "Looking through Drive",
    gws_docs: "Reading the document",
    gws_keep: "Reading notes",
    gws_tasks: "Checking tasks",
    gws_export: "Fetching the document",
    gws_document: "Found it — reading",
    gws_failed: "The request failed. Nothing was changed.",
    gws_empty: "Searched, but found no answer.",
    gws_lost: "No answer came back.",
    gws_unset: "Workspace is not set up on this terminal.",
    gws_slow: "Took too long and was stopped.",
    gws_unsourced: "Could not say which file or message this came from.",
    gws_expired: "Google Workspace access has expired. Please open Settings to reconnect your account.",
    head_plan: "Working it out",
    call_plan: "working out the steps",
    plan_thinking: "Working out the steps…",
    plan_failed: "The request failed. Nothing was changed.",
    plan_empty: "Worked on it, but found no answer.",
    plan_lost: "No answer came back.",
    plan_unset: "The planner is not set up on this terminal.",
    plan_slow: "Took too long and was stopped.",
    call_weather: "checking the weather",
    head_weather: "Weather",
    wx_feels: "Feels like",
    wx_rain: "Rain",
    wx_wind: "Wind",
    wx_humidity: "Humidity",
    wx_sunrise: "Sunrise",
    wx_sunset: "Sunset",
    wx_polar_day: "Midnight sun",
    wx_polar_night: "Polar night",
    cal_free: "Nothing on",
    cal_allday: "All day",
    cal_untitled: "No title",
    cal_maybe: "Unconfirmed",
    cal_now: "Now",
    cal_more: "{n} more",
    call_remember: "remembering that",
    head_remember: "Noted",
    head_calculate: "Calculation",
    remember_known: "{n} remembered",
    remember_dropped: "{n} dropped",
    remember_declined: "That was not kept.",
  },
  ja: {
    ui_language: "言語",
    ui_voice: "声",
    ui_voice_set: "声を {voice} にしました。接続し直します。",
    ui_settings: "設定",
    ui_gws_expired: "Google Workspace の認証期限が切れました。設定画面から再連携してください。",
    ui_status_connecting: "接続中",
    ui_ready: "準備完了",
    ui_offline: "オフライン",
    ui_who: "使う人",
    ui_microphone: "マイク",
    ui_mic_default: "システム既定",
    ui_mic_n: "マイク {n}",
    ui_mic_off: "マイクを切りました",
    ui_listening: "聞いています",
    ui_mic_failed: "マイクを開けませんでした",
    ui_wake_again: "スリープ中：「Hey Gemini」と呼ぶかマイクを押してください。",
    ui_speaker_volume: "スピーカーの音量",
    ui_speaker_volume_at: "スピーカーの音量 {level}",
    ui_speaker_muted: "スピーカーを消音中",
    ui_mute: "消音",
    ui_unmute: "消音を解除",
    ui_speaker_muted_line: "スピーカーを消音しました。聞こえてはいます。",
    ui_speaker_on: "スピーカーを戻しました。",
    ui_interrupt: "割り込み応答",
    ui_interrupt_allow: "発話中の割り込みを許可",
    ui_interrupt_note_on: "割り込み応答を有効にしました。再接続します。",
    ui_interrupt_note_off: "割り込み応答を無効にしました。再接続します。",
    ui_location: "場所",
    ui_where_unset: "未設定 — サーバーの HOME_LOCATION を使っています",
    ui_where_set: "場所を {label} にしました。接続し直します。",
    ui_set_on_map: "地図で設定…",
    ui_no_map: "地図なし: MAPS_API_KEY が未設定か、API を読み込めませんでした。",
    ui_clear: "会話を消す",
    ui_forget: "履歴を消す",
    ui_forget_armed: "もう一度タップで削除",
    ui_forget_failed: "削除できませんでした: {e}。何も消えていません。",
    ui_reboot: "端末を再起動",
    ui_reboot_armed: "もう一度タップで再起動",
    ui_rebooting: "端末を再起動しています…",
    ui_reboot_failed: "再起動できませんでした: {e}。",
    ui_forgot_one: "会話を {n} 件消しました。",
    ui_forgot_many: "会話を {n} 件消しました。",
    ui_forgot_none: "消すものはありませんでした。",
    ui_now_talking: "{who} さんとの会話に切り替えました。",
    ui_search_address: "住所を検索…",
    ui_search: "検索",
    ui_use_position: "現在地を使う",
    ui_drag_map: "地図を動かしてください",
    ui_cancel: "キャンセル",
    ui_set_location: "ここにする",
    ui_no_address: "その住所は見つかりません",
    ui_geocode_failed: "住所を調べられませんでした",
    ui_finding_you: "現在地を探しています…",
    ui_no_position: "ブラウザが現在地を教えてくれませんでした",
    ui_starters: "チップをタップするか、マイクで話しかけてください。例えば",
    ui_starters_basic: "基本機能",
    ui_starters_context: "おすすめ・関連",
    ui_chip_weather: "今日の天気",
    ui_ask_weather: "今日の天気は？",
    ui_chip_weather_of: "{q}の天気",
    ui_ask_weather_of: "{q}の天気は？",
    ui_chip_places: "お店を探す",
    ui_ask_places: "近くのおいしいお店は？",
    ui_chip_places_of: "{q}のお店",
    ui_ask_places_of: "{q}のおすすめのお店は？",
    ui_chip_routes: "ルート",
    ui_ask_routes: "道順を調べたい",
    ui_chip_routes_of: "{q}への行き方",
    ui_ask_routes_of: "{q}への行き方は？",
    ui_chip_web_search: "ウェブ検索",
    ui_ask_web_search: "最新のテックニュースを検索して",
    ui_chip_web_search_of: "{q}を検索",
    ui_ask_web_search_of: "{q}についてウェブ検索して",
    ui_chip_video: "動画",
    ui_ask_video: "動画を見たい",
    ui_chip_video_of: "{q}の動画",
    ui_ask_video_of: "{q}の動画を見せて",
    ui_chip_news: "ニュース",
    ui_ask_news: "今日のニュースは？",
    ui_chip_news_of: "{q}のニュース",
    ui_ask_news_of: "{q}の最新ニュースは？",
    ui_chip_market: "マーケット",
    ui_ask_market: "今日の株式市場は？",
    ui_chip_schedule: "今日の予定",
    ui_ask_schedule: "今日の予定は？",
    ui_chip_mail: "最近のメール",
    ui_ask_mail: "最近のメールを見せて",
    ui_chip_mail_of: "{q}のメール",
    ui_ask_mail_of: "{q}に関するメールを探して",
    ui_chip_docs: "ドキュメント検索",
    ui_ask_docs: "ドライブのドキュメントを探して",
    ui_chip_docs_of: "{q}のドキュメント",
    ui_ask_docs_of: "{q}に関するドキュメントを探して",
    ui_voice_mode: "音声モード",
    ui_type_instead: "入力する…",
    ui_send: "送信",
    ui_working: "調べています…",
    ui_no_answer: "応答がありませんでした",
    ui_lang_note: "画面を日本語にしました。接続し直します。地図のラベルは次回の再読み込みから変わります。",
    source: "出典",
    head_web_search: "ウェブ検索",
    head_image_search: "画像",
    head_youtube_search: "動画",
    head_maps_search: "スポット",
    head_place_info: "スポット",
    head_info: "情報",
    head_camera: "カメラ",
    camera_auth_required: "カメラの再認証が必要です",
    head_home_summary: "家の様子",
    head_home_timeline: "履歴タイムライン",
    near_around: "{where}周辺",
    head_gws: "Workspace",
    head_browse: "ブラウズ",
    browse_step: "ステップ {n}",
    browse_failed: "そのページを読めませんでした。",
    browse_lost: "場所が変わったため中断しました。",
    call_browse: "ブラウザを開いています",
    on_the_way: "途中の{q}",
    mode_DRIVE: "車",
    mode_WALK: "徒歩",
    mode_BICYCLE: "自転車",
    mode_TRANSIT: "公共交通",
    call_place_info: "調べています",
    call_web_search: "検索しています",
    call_image_search: "画像を探しています",
    call_youtube_search: "動画を探しています",
    call_maps_search: "地図を検索しています",
    call_maps_route: "ルートを調べています",
    call_maps_search_along_route: "ルート沿いを探しています",
    call_gws: "Workspace に問い合わせ中",
    gws_calendar: "カレンダーを見ています",
    gws_mail: "メールを検索しています",
    gws_drive: "ドライブを探しています",
    gws_docs: "ドキュメントを読んでいます",
    gws_keep: "メモを読んでいます",
    gws_tasks: "ToDo を確認しています",
    gws_export: "ドキュメントを取得しています",
    gws_document: "見つかりました。読んでいます",
    gws_failed: "リクエストが失敗しました。変更は行われていません",
    gws_empty: "検索しましたが、答えは見つかりませんでした",
    gws_lost: "応答がありませんでした",
    gws_unset: "この端末では Workspace が設定されていません",
    gws_slow: "時間がかかりすぎたため中断しました",
    gws_unsourced: "どのファイル・メールから得た情報かは特定できませんでした",
    gws_expired: "Google Workspace の認証期限が切れました。設定画面から再連携してください。",
    head_plan: "順番に調べています",
    call_plan: "手順を組み立てています",
    plan_thinking: "手順を組み立てています…",
    plan_failed: "リクエストが失敗しました。変更は行われていません",
    plan_empty: "調べましたが、答えは見つかりませんでした",
    plan_lost: "応答がありませんでした",
    plan_unset: "この端末ではプランナーが設定されていません",
    plan_slow: "時間がかかりすぎたため中断しました",
    call_weather: "天気を調べています",
    head_weather: "天気",
    wx_feels: "体感",
    wx_rain: "降水",
    wx_wind: "風",
    wx_humidity: "湿度",
    wx_sunrise: "日の出",
    wx_sunset: "日の入り",
    wx_polar_day: "白夜",
    wx_polar_night: "極夜",
    cal_free: "予定なし",
    cal_allday: "終日",
    cal_untitled: "タイトルなし",
    cal_maybe: "未確定",
    cal_now: "いま",
    cal_more: "ほか{n}件",
    call_remember: "覚えています",
    head_remember: "覚えました",
    head_calculate: "計算",
    remember_known: "記憶{n}件",
    remember_dropped: "{n}件を破棄",
    remember_declined: "覚えられませんでした。",
  },
};

/**
 * Look up one string in the language the terminal is set to.
 *
 * Three falls, in order: the locale's own block, English, then whatever the
 * caller offered. A key present in `en` and missing from `ja` shows in
 * English rather than as a blank card heading, which is why the parity case in
 * `js_check` is worth having — nothing at runtime will tell you.
 */
export function t(key, fallback = "") {
  const base = (document.documentElement.lang || "").split("-")[0].toLowerCase();
  return (STRINGS[base] || STRINGS.en)[key] ?? STRINGS.en[key] ?? fallback;
}

/**
 * Which locale to open in, given what is saved and what the browser reports.
 *
 * Saved first, and there is normally a saved value: the settings sheet writes
 * one the first time anybody touches the picker. Before that the kiosk's own
 * locale decides, which is the same source `loadMaps` has always trusted for
 * the map tiles and is a thing a person set up once rather than something a
 * model guessed this turn. Neither one recognised leaves English.
 *
 * Pure, and separate from `setLocale`, because it is the half worth checking:
 * a terminal that comes up in the wrong language is not something a case can
 * observe once the attribute has been written.
 */
export function pickLocale(saved, browser = "") {
  for (const candidate of [saved, browser]) {
    const base = String(candidate || "").split("-")[0].toLowerCase();
    if (LOCALES.includes(base)) return base;
  }
  return "en";
}

/** Put the chosen locale where `t`, `Intl` and the font picker all read it. */
export function setLocale(locale) {
  document.documentElement.lang = locale;
}

/**
 * Translate the markup.
 *
 * The strings are in `index.html` in English rather than left blank and filled
 * in from here, for the reason the clock face is in the markup: the kiosk
 * paints before `app.js` has loaded, and a settings sheet of empty buttons is
 * worse than one in the wrong language for 200 ms. So an English terminal
 * never needs this pass to have run, and a Japanese one gets its labels
 * swapped on the first frame after the module lands.
 *
 * Four attributes rather than one, because three of these are not text nodes:
 * a `title` on an icon button, a `placeholder` in an input, and the
 * `data-prompt` a starter chip sends — which has to travel with its label, or
 * a Japanese chip sends an English sentence and is answered in English.
 */
export function applyStatic(root = document) {
  for (const [attr, apply] of [
    ["data-i18n", (el, s) => { el.textContent = s; }],
    ["data-i18n-title", (el, s) => { el.title = s; }],
    ["data-i18n-placeholder", (el, s) => { el.placeholder = s; }],
    ["data-i18n-prompt", (el, s) => { el.dataset.prompt = s; }],
    // One word rather than `data-i18n-aria-label`, because the check that
    // matches the markup's keys against this table reads them with
    // `data-i18n(?:-\w+)?=`, and a second hyphen falls out of that — a key
    // nothing applies and nothing notices is the failure this table exists
    // to make impossible.
    ["data-i18n-arialabel", (el, s) => { el.ariaLabel = s; }],
  ]) {
    for (const el of root.querySelectorAll(`[${attr}]`)) {
      const key = el.getAttribute(attr);
      const text = t(key);
      if (text) apply(el, text);
    }
  }
}
