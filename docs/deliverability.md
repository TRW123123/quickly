# Zustellbarkeit: Postmaster Tools, SNDS und die Absenderregeln

Diese Anleitung erklärt, wie du siehst, ob deine Mails bei Gmail und Outlook gut ankommen, und welche Regeln die großen Postfachanbieter an Absender stellen. Du brauchst dafür keine Programmierkenntnisse. An einer Stelle musst du selbst einen Eintrag im DNS deiner Domain bei Strato setzen; das beschreibt der Abschnitt zu Google.

## 1. Google Postmaster Tools

Google Postmaster Tools ist ein kostenloses Dashboard von Google. Es zeigt dir, wie Gmail die Mails deiner Domain bewertet: wie viele Empfänger sie als Spam markieren, wie es um den Ruf deiner Domain steht und ob die Absender-Prüfungen (SPF, DKIM, DMARC) klappen.

**Adresse:** <https://postmaster.google.com>

### Einrichten

1. Melde dich unter <https://postmaster.google.com> mit einem Google-Konto an. Ein normales Gmail-Konto reicht.
2. Füge deine Absenderdomain hinzu. Gemeint ist die Domain hinter dem @ in deiner Absenderadresse, also zum Beispiel `deinefirma.de`.
3. Google zeigt dir danach einen TXT-Eintrag an. Er beginnt mit `google-site-verification=` und dahinter steht eine lange Zeichenfolge.
4. Diesen Eintrag setzt du selbst bei Strato. Im Strato-Kundenbereich gehst du zu den Domain-Einstellungen und dort zur DNS-Verwaltung. Lege einen neuen TXT-Eintrag für die Domain selbst an (nicht für eine Subdomain) und füge den Text von Google genau so ein. Bestehende TXT-Einträge wie SPF bleiben stehen; ein zusätzlicher TXT-Eintrag stört sie nicht.
5. Zurück in Postmaster Tools klickst du auf „Bestätigen“. Neue DNS-Einträge brauchen manchmal ein paar Stunden, bis sie überall sichtbar sind. Klappt die Bestätigung nicht sofort, versuche es später noch einmal.

Den TXT-Eintrag lässt du dauerhaft stehen. Wird er gelöscht, verlierst du den Zugang zu den Daten.

### Was die Dashboards zeigen

Wichtig vorab: Google zeigt nur dann Daten an, wenn deine Domain täglich eine nennenswerte Menge Mails an Gmail-Adressen schickt. Google nennt dafür keine genaue Zahl. Bei wenigen Mails am Tag bleiben die Diagramme leer; das ist kein Fehler.

- **Spamrate:** Der Anteil deiner Mails, den Gmail-Nutzer selbst mit „Spam melden“ markiert haben. Das ist die wichtigste Zahl.
- **Domain-Reputation und IP-Reputation:** Wie Gmail den Ruf deiner Domain und der versendenden Server einschätzt, in Stufen von „Schlecht“ bis „Hoch“. Google hat die Oberfläche 2025 umgebaut; je nach Version deines Kontos siehst du diese Ansicht noch oder stattdessen eine Übersicht, ob du die Absenderregeln erfüllst („Compliance-Status“).
- **Authentifizierung:** Welcher Anteil deiner Mails SPF, DKIM und DMARC bestanden hat.
- **Verschlüsselung:** Ob die Mails verschlüsselt (TLS) übertragen wurden.
- **Zustellfehler:** Wie viele Mails Gmail abgelehnt oder vorübergehend zurückgewiesen hat.

### Die beiden Grenzwerte

Google gibt für die Spamrate zwei Schwellen vor:

- **Unter 0,1 %** solltest du dauerhaft bleiben. Das heißt: weniger als eine Spam-Meldung auf 1.000 Mails.
- **0,3 % oder mehr** darfst du nie erreichen. Ab dort filtert Gmail deine Mails deutlich stärker in den Spam-Ordner oder lehnt sie ab, und die Erholung dauert lange.

Steigt die Spamrate über 0,1 %, solltest du die Menge drosseln, die Empfängerliste prüfen und den Text der Kampagne überarbeiten, bevor du weiter versendest.

## 2. Microsoft SNDS und JMRP

Microsoft bietet für Outlook.com, Hotmail und Live zwei kostenlose Programme an:

- **SNDS (Smart Network Data Services)** zeigt, wie viele Mails von einer IP-Adresse bei Outlook.com angekommen sind, wie viele davon als Spam gefiltert wurden und wie viele Beschwerden es gab.
- **JMRP (Junk Mail Reporting Program)** schickt dir eine Kopie, wenn ein Outlook.com-Nutzer eine deiner Mails als Junk markiert.

**Adresse:** <https://sendersupport.olc.protection.outlook.com/snds/>

### Ehrliche Einschätzung für den Versand über Strato

Beide Programme arbeiten mit **IP-Adressen**, nicht mit Domains. Man meldet sich mit dem IP-Bereich an, von dem die Mails verschickt werden, und Microsoft prüft dann, ob man diese IPs wirklich verwaltet. Die Bestätigung geht an Adressen, die zum Inhaber der IP gehören.

Wenn du über den SMTP-Server von Strato versendest, gehen deine Mails über **Strato-Server mit Strato-IPs**, die sich viele Kunden teilen. Diese IPs gehören nicht dir. Du kannst sie deshalb nicht bei SNDS oder JMRP anmelden, und selbst wenn du die Zahlen sehen könntest, würden sie den Versand aller Strato-Kunden auf diesen Servern zeigen und nicht deinen eigenen.

SNDS und JMRP lohnen sich erst, wenn du später von eigenen IP-Adressen versendest, also zum Beispiel über einen eigenen Mailserver oder eine dedizierte IP bei einem Versanddienstleister. Bis dahin ist Google Postmaster Tools das Werkzeug, das dir wirklich etwas über deine Domain sagt.

## 3. SPF, DKIM und DMARC: warum die Domain übereinstimmen muss

Diese drei Einträge im DNS beweisen den Empfängern, dass eine Mail wirklich von dir kommt.

- **SPF** listet die Server auf, die für deine Domain Mails verschicken dürfen. Beim Versand über Strato muss Strato darin stehen. Den genauen Eintrag nennt die Strato-Hilfe; übernimm ihn von dort, statt ihn abzutippen.
- **DKIM** ist eine digitale Unterschrift, die der versendende Server an jede Mail hängt. Der passende öffentliche Schlüssel liegt als DNS-Eintrag bei deiner Domain. Prüfe im Strato-Kundenbereich, ob DKIM für deine Domain angeboten wird und eingeschaltet ist.
- **DMARC** sagt den Empfängern, was sie tun sollen, wenn SPF und DKIM nicht passen, und schickt dir auf Wunsch Berichte. Für den Anfang reicht ein Eintrag mit `p=none`, der nur beobachtet und nichts blockiert. Er wird als TXT-Eintrag unter `_dmarc.deinefirma.de` angelegt, zum Beispiel `v=DMARC1; p=none; rua=mailto:dmarc@deinefirma.de`.

Der entscheidende Punkt ist die **Übereinstimmung (Alignment)**. DMARC ist nur dann bestanden, wenn mindestens eine der beiden Prüfungen klappt **und** zur Domain in deiner sichtbaren Absenderadresse passt:

- SPF zählt für DMARC nur, wenn die technische Absenderdomain (Return-Path) zur Domain im Absenderfeld passt.
- DKIM zählt für DMARC nur, wenn die Unterschrift mit deiner eigenen Domain (`d=deinefirma.de`) erstellt wurde und nicht mit der Domain des Anbieters.

Ob alles stimmt, siehst du im Kopf einer empfangenen Mail. In Gmail öffnest du dafür eine Testmail an dich selbst und wählst „Original anzeigen“; dort steht für SPF, DKIM und DMARC jeweils „PASS“ oder „FAIL“. In Postmaster Tools zeigt das Dashboard „Authentifizierung“ dasselbe über alle Mails.

## 4. Regeln von Gmail und Yahoo für Massenversender

Seit Februar 2024 gelten bei Gmail und Yahoo strengere Regeln. Als Massenversender gilt bei Gmail, wer an einem Tag rund 5.000 oder mehr Mails an Gmail-Adressen schickt. Microsoft hat für Outlook.com 2025 vergleichbare Anforderungen eingeführt.

Für **alle** Absender gilt:

- SPF oder DKIM muss eingerichtet sein.
- Die Mails müssen verschlüsselt (TLS) übertragen werden; das erledigt Strato.
- Die Spamrate muss unter 0,3 % bleiben.

Für **Massenversender** kommt hinzu:

- SPF **und** DKIM müssen eingerichtet sein.
- Ein DMARC-Eintrag muss vorhanden sein (mindestens `p=none`), und die Absenderdomain muss mit SPF oder DKIM übereinstimmen (siehe Abschnitt 3).
- Werbe- und Abo-Mails brauchen eine **Abmeldung mit einem Klick** (nach RFC 8058). Der Empfänger klickt im Mailprogramm auf „Abbestellen“ und ist sofort abgemeldet, ohne sich irgendwo einzuloggen oder etwas zu bestätigen. Abmeldungen müssen innerhalb von zwei Tagen umgesetzt sein.
- Die Spamrate muss unter 0,3 % bleiben; Google empfiehlt dauerhaft unter 0,1 %.

In Quickly ist die Abmeldung mit einem Klick pro Kampagne einstellbar und standardmäßig eingeschaltet. Lass sie an, auch wenn du unter der Schwelle von 5.000 Mails am Tag bleibst: Ein einfacher Abmeldeweg führt dazu, dass Empfänger abbestellen, statt „Spam melden“ zu klicken, und genau diese Spam-Meldungen drücken die Spamrate.

Offizielle Seiten:

- Gmail: <https://support.google.com/a/answer/81126>
- Yahoo: <https://senders.yahooinc.com/best-practices/>

## 5. Adressprüfung beim Import in Quickly

Quickly prüft jede Adresse beim Import kostenlos und ohne externen Dienst:

- **Schreibweise:** Adressen wie `max@@firma` oder `max@firma` werden nicht importiert.
- **Mailserver der Domain:** Quickly fragt im DNS nach, ob die Domain überhaupt Mails annimmt (MX-Eintrag, ersatzweise ein A-Eintrag). Nimmt die Domain keine Mails an, wird die Adresse nicht importiert. Ist der DNS gerade nicht erreichbar, wird die Adresse trotzdem importiert und als „unbekannt“ markiert.
- **Wegwerf-Adressen** (zum Beispiel von Mailinator oder 10minutemail) werden nicht importiert.
- **Funktionsadressen** wie `info@`, `kontakt@` oder `buchhaltung@` werden importiert, aber markiert. Bei solchen Adressen liest oft niemand Bestimmtes mit, und sie melden häufiger Spam.

Die Prüfung sagt nicht, ob es das einzelne Postfach wirklich gibt; das lässt sich ohne Versand oder Bezahldienst nicht zuverlässig feststellen. Sie fängt aber Tippfehler, tote Domains und Wegwerf-Adressen ab, also genau die Fälle, die zu Rückläufern führen und deinen Ruf bei den Postfachanbietern verschlechtern.
