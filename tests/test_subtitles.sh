#!/bin/sh
# Subtitle track tests for the ani-cli client.
#
# The script is sourced with ANI_CLI_LIB_ONLY=1, so the real functions under test
# run and nothing else does. Fixtures are subtitle arrays captured from the live
# provider (5 tracks, 9 tracks) plus the shapes that have to degrade gracefully.
#
# Run: sh tests/test_subtitles.sh

set -u

cd "$(dirname "$0")/.." || exit 1
ANI_CLI_LIB_ONLY=1
export ANI_CLI_LIB_ONLY
. ./ani-cli

pass=0
fail=0

check() { # $1 = description, $2 = expected, $3 = actual
    if [ "$2" = "$3" ]; then
        pass=$((pass + 1))
        printf 'ok   %s\n' "$1"
    else
        fail=$((fail + 1))
        printf 'FAIL %s\n     expected: %s\n     actual:   %s\n' "$1" "$2" "$3"
    fi
}

FX="tests/fixtures"

# ---------------------------------------------------------------- display names

check_name() { # $1 = provider label, $2 = expected display name
    check "display name for '$1'" "$2" "$(subtitle_display_name "$1")"
}

check_name "English" "English"
check_name "Japanese" "Japanese"
check_name "Thai" "Thai"
check_name "English 2" "English (2)"
check_name "English (- (Crunchyroll))" "English (Crunchyroll)"
check_name "Portuguese (- Portuguese(Brazil))" "Portuguese (Brazil)"
check_name "Chinese - Chinese (Simplified)" "Chinese (Simplified)"
check_name "Spanish (- Spanish(Latin America))" "Spanish (Latin America)"
check_name "Norwegian (Bokmål)" "Norwegian (Bokmål)"
check_name "Signs & Songs" "Signs & Songs"
check_name "" "Unknown Language"

# ---------------------------------------------------------------- track parsing

# 9 real tracks, provider order, every language kept
nine=$(subtitle_tracks <"$FX/embed_solo_s2e2_dub.json" | subtitle_unique)
check "nine tracks are parsed" "9" "$(printf '%s\n' "$nine" | grep -c .)"
check "nine titles" \
    "Arabic English French German Italian Portuguese Russian Spanish Spanish (2)" \
    "$(printf '%s\n' "$nine" | cut -f 1 | paste -sd' ' -)"
check "provider lang is preserved but unused for naming" "en" \
    "$(printf '%s\n' "$nine" | cut -f 2 | sort -u | paste -sd' ' -)"
check "the provider default is marked once" "1" \
    "$(printf '%s\n' "$nine" | cut -f 3 | grep -c 1)"
check "every track has its own url" "9" \
    "$(printf '%s\n' "$nine" | cut -f 4 | sort -u | grep -c .)"

# 5 real tracks, three of them English: none may be dropped or share a title
five=$(subtitle_tracks <"$FX/embed_aot_e1_sub.json" | subtitle_unique)
check "five tracks are parsed" "5" "$(printf '%s\n' "$five" | grep -c .)"
check "same-language tracks are distinguished" \
    "English English (2) English (Crunchyroll) Portuguese Spanish" \
    "$(printf '%s\n' "$five" | cut -f 1 | paste -sd' ' -)"

# one track
one=$(printf '%s' '{"subtitles":[{"lang":"en","label":"English","default":true,"src":"https://h/a.vtt"}]}' | subtitle_tracks | subtitle_unique)
check "a single track is kept" "1" "$(printf '%s\n' "$one" | grep -c .)"

# no subtitles at all
empty=$(printf '%s' '{"src":"https://h/master.m3u8","subtitles":[],"poster":"x"}' | subtitle_tracks | subtitle_unique)
check "no subtitles yields no lines" "" "$empty"

# no subtitles key at all (must not fall through to other "src" fields)
missing=$(printf '%s' '{"src":"https://h/master.m3u8","poster":"x"}' | subtitle_tracks | subtitle_unique)
check "missing subtitles key yields no lines" "" "$missing"

# duplicate rows of the same url collapse
dupes='{"subtitles":[{"lang":"en","label":"English","default":true,"src":"https://h/a.vtt"},{"lang":"en","label":"English","default":true,"src":"https://h/a.vtt"}]}'
deduped=$(printf '%s' "$dupes" | subtitle_tracks | subtitle_unique)
check "duplicate urls collapse" "1" "$(printf '%s\n' "$deduped" | grep -c .)"

# a track with no src is not a playable row
nosrc='{"subtitles":[{"lang":"en","label":"Broken","default":false},{"lang":"en","label":"English","default":true,"src":"https://h/a.vtt"}]}'
nosrcrows=$(printf '%s' "$nosrc" | subtitle_tracks | subtitle_unique)
check "tracks without a url are skipped" "English" "$(printf '%s\n' "$nosrcrows" | cut -f 1)"

# ---------------------------------------------------------------- format detection

tmpdir=$(mktemp -d)
printf 'WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhi\n' >"$tmpdir/a.vtt"
printf '1\n00:00:01,000 --> 00:00:02,000\nhi\n' >"$tmpdir/b.srt"
printf '[Script Info]\n\n[Events]\nDialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,hi\n' >"$tmpdir/c.ass"
printf 'not a subtitle' >"$tmpdir/d.txt"
printf '' >"$tmpdir/e.vtt"

check "webvtt is detected from content" "vtt" "$(subtitle_file_format "$tmpdir/a.vtt")"
check "srt is detected from content" "srt" "$(subtitle_file_format "$tmpdir/b.srt")"
check "ass is detected from content" "ass" "$(subtitle_file_format "$tmpdir/c.ass")"
check "unknown content is not labelled playable" "" "$(subtitle_file_format "$tmpdir/d.txt")"
check "empty content is not labelled playable" "" "$(subtitle_file_format "$tmpdir/e.vtt")"

check "extension from url" "vtt" "$(subtitle_extension 'https://h/a.vtt')"
check "extension ignores query" "srt" "$(subtitle_extension 'https://h/a.srt?t=1')"
check "no extension is unknown" "" "$(subtitle_extension 'https://h/track')"
check "ssa maps to ass" "ass" "$(subtitle_extension 'https://h/a.ssa')"

# ---------------------------------------------------------------- track selection

sub_tracks=$nine
sub_ready=""
while IFS='	' read -r name lang is_default path; do
    [ -n "$path" ] || continue
    sub_ready="$sub_ready$name	$lang	$is_default	$tmpdir/$name.vtt
"
done <<EOF
$nine
EOF

sub_lang_pref="ja"
select_subtitle_sid
check "unmatched preference falls back to the provider default" "2" "$sub_sid"

sub_lang_pref="spanish"
select_subtitle_sid
check "preference matches a language name" "8" "$sub_sid"

# the provider sends lang="en" on every track, so a bare code cannot match here;
# the name match above is what works against this provider's data
sub_lang_pref="es"
select_subtitle_sid
check "a code that its lang field never carries falls back to the default" "2" "$sub_sid"

# a provider that does send real codes is matched on the code
sub_ready_codes="Arabic	ar	0	/tmp/Arabic.vtt
English	en	1	/tmp/English.vtt
German	de	0	/tmp/German.vtt
"
sub_ready_saved=$sub_ready
sub_ready=$sub_ready_codes
sub_lang_pref="de"
select_subtitle_sid
check "preference matches a language code" "3" "$sub_sid"
sub_lang_pref="ja"
select_subtitle_sid
check "an unmatched code falls back to the provider default" "2" "$sub_sid"
sub_ready=$sub_ready_saved

sub_lang_pref=""
select_subtitle_sid
check "no preference uses the provider default, not the first track" "2" "$sub_sid"

# a provider that marks no default and a user with no preference: subtitles off
sub_ready="English	en	0	$tmpdir/a.vtt
Japanese	ja	0	$tmpdir/a.vtt
"
sub_lang_pref=""
select_subtitle_sid
check "no default and no preference leaves subtitles off" "no" "$sub_sid"

sub_lang_pref="japanese"
select_subtitle_sid
check "explicit preference still selects" "2" "$sub_sid"

# ---------------------------------------------------------------- files on disk

sub_ready="Arabic	ar	0	$tmpdir/a.vtt
English	en	1	$tmpdir/b.srt
"
write_sub_config "$tmpdir/sub.conf"
check "config lists every readable track" "2" "$(grep -c '^sub-file=' "$tmpdir/sub.conf")"
check "config preselects the chosen track" "sid=2" "$(grep '^sid=' "$tmpdir/sub.conf")"
check "config keeps paths with spaces intact" "sub-file=$tmpdir/a.vtt" \
    "$(sed -n '1p' "$tmpdir/sub.conf")"

check "comma list for list-style players" "$tmpdir/a.vtt,$tmpdir/b.srt" "$(subtitle_file_list)"

rm -rf "$tmpdir"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
