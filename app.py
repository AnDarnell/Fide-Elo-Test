import time

import pandas as pd
import streamlit as st

import cache
from fide_api import (
    RATING_TYPES,
    FideBlocked,
    FideError,
    MAX_DELAY,
    MIN_DELAY,
    calc_performance_rating,
    calc_weighted_performance,
    fetch_last_n_games,
    get_player_profile,
    sort_newest_first,
)

COOLDOWN_SECONDS = 15 * 60

st.set_page_config(page_title="FIDE Stats", page_icon="♟️", layout="wide")
st.markdown(
    """
    <style>
      .block-container {max-width: 1440px; padding-top: 2rem; padding-bottom: 3rem;}
      h1 {letter-spacing: -0.035em;}
      div[data-testid="stMetric"] {
        background: var(--secondary-background-color);
        border: 1px solid rgba(128, 128, 128, .16);
        padding: 1rem 1.15rem;
        border-radius: 12px;
      }
      div[data-testid="stMetricLabel"] {font-size: .84rem;}
      div[data-testid="stMetricValue"] {font-size: 1.55rem;}
      div[data-testid="stTabs"] button {font-weight: 600;}
      .app-subtitle {color: #7b8491; margin-top: -.8rem; margin-bottom: 1.4rem;}
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("♟️ FIDE Elo Test")
st.markdown('<p class="app-subtitle">Skriv in ett Fide-ID i rutan under för att testa. Kör olika värden i analysinställningarna och antal partier till höger. Antal partier skulle kunna variera likt k-faktor gör nu etc.</p>', unsafe_allow_html=True)

now = time.time()
blocked_until = st.session_state.get("blocked_until", 0)
blocked = blocked_until > now

# Main controls stay visible at the top; less-used analysis settings are tucked away.
with st.container(border=True):
    with st.form("player_controls"):
        id_col, type_col, games_col, action_col = st.columns([2.1, 1.25, 1.4, 1.2], vertical_alignment="bottom")
        fide_id_input = id_col.text_input("FIDE-ID", placeholder="Till exempel 1503014")
        rating_type = type_col.selectbox("Ratingtyp", options=[0, 1, 2], format_func=lambda x: RATING_TYPES[x])
        n_games = games_col.slider("Antal partier", min_value=5, max_value=100, value=50, step=5)
        fetch_btn = action_col.form_submit_button(
            "Hämta data", type="primary", use_container_width=True, disabled=blocked
        )

    with st.expander("Analysinställningar", expanded=False):
        setting_cols = st.columns([1, 1, 1.2, 1.4])
        window = setting_cols[0].slider("Rullande fönster", min_value=3, max_value=20, value=10, step=1)
        w_window = setting_cols[1].slider("Viktat fönster (antal partier)", min_value=10, max_value=50, value=30, step=5)
        w_oldest = setting_cols[2].slider("Vikt för äldsta partiet", min_value=0.0, max_value=1.0, value=0.2, step=0.05)
        skip_unrated = setting_cols[3].checkbox("Hoppa över orankade motståndare (Ja)", value=True)

    id_candidate = fide_id_input.strip()
    if id_candidate.isdigit():
        cache_col, clear_col, note_col = st.columns([1.25, 1, 3], vertical_alignment="center")
        cached_at = cache.cached_at(id_candidate)
        if cached_at:
            cache_col.caption(f"Cache uppdaterad · {cached_at[:16].replace('T', ' ')}")
            if clear_col.button("Rensa cache", use_container_width=True):
                cache.clear(id_candidate)
                st.rerun()
        else:
            cache_col.caption("Ingen sparad cache")
        note_col.caption(f"FIDE-anrop väntar {MIN_DELAY:.0f}–{MAX_DELAY:.0f} sekunder för att undvika blockering - kan ta en stund.")

if blocked:
    mins = int((blocked_until - now) // 60) + 1
    st.warning(f"FIDE verkar strypa din IP. Hämtning är spärrad i cirka {mins} minuter.")

if not fide_id_input.strip().isdigit():
    st.info("Ange ett FIDE-ID för att visa spelarens statistik.")
    st.stop()

fide_id = fide_id_input.strip()

if fetch_btn:
    progress = st.progress(0.0, text="Startar hämtning …")

    def on_progress(done: int, target: int, msg: str) -> None:
        progress.progress(min(done / target, 1.0), text=msg)

    try:
        result = fetch_last_n_games(fide_id, n_games, rating_type, progress=on_progress)
    except FideBlocked as e:
        progress.empty()
        st.session_state["blocked_until"] = time.time() + COOLDOWN_SECONDS
        st.error(f"{e}\n\nHämtningen är spärrad i {COOLDOWN_SECONDS // 60} minuter. Perioder som redan hunnit hämtas är sparade på disk.")
        st.stop()
    except FideError as e:
        progress.empty()
        st.error(str(e))
        st.stop()
    except Exception as e:
        progress.empty()
        st.error(f"Oväntat fel: {e}")
        st.stop()

    progress.empty()
    if not result["games"]:
        st.warning("Inga partier hittades, så ingenting cachades.")
        st.stop()

    cache.save(fide_id, {"periods": result["periods"], "games": result["games"]})
    st.success(f"Hämtade {len(result['games'])} partier från {result['periods_fetched']} perioder.")
    for warning in result.get("warnings", []):
        st.warning(warning)

player_data = cache.load(fide_id)
if not player_data:
    st.info("Ingen cachad data ännu. Hämta data för att komma igång.")
    st.stop()

player_name = player_data.get("player_name")
current_ratings = player_data.get("current_ratings", {})
rating_label = RATING_TYPES[rating_type].title()
lookup_key = f"player_profile_lookup_v2_done_{fide_id}"
if (not player_name or rating_label not in current_ratings) and not st.session_state.get(lookup_key):
    st.session_state[lookup_key] = True
    try:
        profile = get_player_profile(fide_id)
        player_name = profile.get("name") or player_name
        current_ratings = profile.get("ratings", {})
        if player_name:
            player_data["player_name"] = player_name
        if current_ratings:
            player_data["current_ratings"] = current_ratings
        if player_name or current_ratings:
            cache.save(fide_id, player_data)
    except FideBlocked as e:
        st.session_state["blocked_until"] = time.time() + COOLDOWN_SECONDS
        st.warning(f"Spelarprofilen kunde inte hämtas eftersom FIDE begränsar anropen. {e}")
    except FideError as e:
        st.warning(f"Spelarprofilen kunde inte hämtas just nu. {e}")

st.subheader(f"{player_name or 'FIDE-spelare'} · {fide_id}")

all_games = player_data.get("games", [])
games = [g for g in all_games if g["type"] == RATING_TYPES[rating_type]]
if skip_unrated:
    games = [g for g in games if g["opp_rating"] > 0]
games = sort_newest_first(games)[:n_games]

if not games:
    st.warning(f"Inga {RATING_TYPES[rating_type]}-partier finns i cachen för den här spelaren. Hämta data för den ratingtypen.")
    st.stop()

total_score = sum(g["score"] for g in games)
total_games = sum(g["n_games"] for g in games)
wins = sum(1 for g in games if g["result"] == "Win")
draws = sum(1 for g in games if g["result"] == "Draw")
losses = sum(1 for g in games if g["result"] == "Loss")
rated_games = [g for g in games if g["opp_rating"] > 0]
rated_n = sum(g["n_games"] for g in rated_games)
avg_opp = sum(g["opp_rating"] * g["n_games"] for g in rated_games) / rated_n if rated_n else 0
total_rating_change = sum(g["rating_change"] for g in games)
perf_rating = calc_performance_rating(games)

st.caption(f"{RATING_TYPES[rating_type]} · dataurval")
summary_cols = st.columns(5)
summary_cols[0].metric("Partier i dataurvalet", total_games)
summary_cols[1].metric("Poäng", f"{total_score:g}/{total_games}")
summary_cols[2].metric("Snittmotstånd", f"{avg_opp:.0f}")
summary_cols[3].metric("Prestationsrating", f"{perf_rating:.0f}" if perf_rating else "N/A")
summary_cols[4].metric("Ratingförändring", f"{total_rating_change:+.1f}")

wp = calc_weighted_performance(games, w_window, 1.0, w_oldest)
if wp and wp["n_games"] >= 5:
    st.markdown("#### Viktad form - Ny elo")
    weighted_cols = st.columns(3)
    proposed_elo = round(wp["rating"])
    current_elo = current_ratings.get(rating_label)
    if current_elo is not None:
        elo_delta = proposed_elo - int(current_elo)
        magnitude = min(abs(elo_delta) / 100, 1)
        neutral = (232, 190, 65)
        target = (213, 70, 78) if elo_delta < 0 else (48, 157, 105)
        color_rgb = tuple(round(neutral[i] + (target[i] - neutral[i]) * magnitude) for i in range(3))
        elo_color = "#%02x%02x%02x" % color_rgb
        elo_value = f"{proposed_elo} ({elo_delta:+d})"
    else:
        elo_color = "#88909b"
        elo_value = f"{proposed_elo}"
    weighted_cols[0].markdown(
        f"<div style='padding:1rem 1.15rem;border:1px solid rgba(128,128,128,.16);"
        f"border-radius:12px;background:var(--secondary-background-color);'>"
        f"<div style='font-size:.84rem;color:var(--text-color);opacity:.75;'>"
        f"Föreslagen Elo · senaste {w_window}</div>"
        f"<div style='font-size:1.55rem;font-weight:600;color:{elo_color};margin-top:.25rem;'>"
        f"{elo_value}</div>"
        f"<div style='font-size:.75rem;color:var(--text-color);opacity:.65;margin-top:.2rem;'>"
        f"Nuvarande {rating_label}: {current_elo if current_elo is not None else 'saknas'}</div></div>",
        unsafe_allow_html=True,
    )
    weighted_cols[1].metric("Viktad poängprocent", f"{wp['pct'] * 100:.1f}%")
    weighted_cols[2].metric(f"Rankade partier i fönstret", wp["n_games"])
    st.caption(
        f"Beräkningen använder de {wp['n_games']} senaste rankade partierna, högst {w_window}. "
        f"Vikten faller linjärt från 1,0 för det senaste till {w_oldest:.2f} för det äldsta."
    )

results_tab, games_tab, form_tab, rating_tab = st.tabs(["Resultat", "Partier", "Form", "Ratingutveckling"])

with results_tab:
    st.subheader("Resultatfördelning")
    result_cols = st.columns(3)
    result_cols[0].metric("Vinster", wins)
    result_cols[1].metric("Remier", draws)
    result_cols[2].metric("Förluster", losses)

with games_tab:
    st.subheader(f"Senaste {len(games)} partierna")
    df = pd.DataFrame(games)
    df = df[["period", "date", "tournament", "opponent", "title", "opp_rating", "result", "score", "rating_change"]]
    df.columns = ["Period", "Datum", "Turnering", "Motståndare", "Titel", "Mots. rating", "Resultat", "Poäng", "Rating Δ"]

    def color_result(value):
        colors = {
            "Win": "background-color: #d4edda",
            "Draw": "background-color: #fff3cd",
            "Loss": "background-color: #f8d7da",
        }
        return colors.get(value, "")

    try:
        styled_df = df.style.map(color_result, subset=["Resultat"])
    except AttributeError:
        styled_df = df.style.applymap(color_result, subset=["Resultat"])
    st.dataframe(styled_df, use_container_width=True, hide_index=True)

games_chrono = list(reversed(games))
rolling_pr = []
weighted_pr = []
for i in range(len(games_chrono)):
    start = max(0, i - window + 1)
    window_games = games_chrono[start : i + 1]
    rolling_pr.append(calc_performance_rating(window_games) if len(window_games) >= 3 else None)
    wpi = calc_weighted_performance(list(reversed(games_chrono[: i + 1])), w_window, 1.0, w_oldest)
    weighted_pr.append(wpi["rating"] if wpi and wpi["n_games"] >= 5 else None)

with form_tab:
    st.subheader(f"Rullande performance rating · {window} partier")
    df_rolling = pd.DataFrame({
        "Parti": range(1, len(games_chrono) + 1),
        "Rullande PR": rolling_pr,
        "Viktad PR": weighted_pr,
    }).set_index("Parti").dropna(how="all")
    st.line_chart(df_rolling)

with rating_tab:
    st.subheader("Kumulativ ratingförändring")
    df_chart = pd.DataFrame({"Rating Δ": [g["rating_change"] for g in games_chrono]})
    df_chart["Kumulativ Δ"] = df_chart["Rating Δ"].cumsum()
    df_chart.index = range(1, len(games_chrono) + 1)
    df_chart.index.name = "Parti"
    st.line_chart(df_chart["Kumulativ Δ"])
