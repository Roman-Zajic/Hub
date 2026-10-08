export const meta = {
    title: 'Email Composer'
};

/* Email Composer — same look as Notes: every class below comes from styles.css
   (.banner, .seg, .toolbar, .split, .editor-textarea, .preview-frame, .btn-*). No CSS in this file.
   The markup parser / email HTML generator further down is unchanged. */

export async function render(root, App) {
    root.innerHTML = `
        <div class="editor-panel fill">
            <div class="banner">
                <div class="banner-main">
                    <span id="ec-title" class="banner-title static" title="Taken from the first # heading"></span>
                </div>
                <div class="banner-actions">
                    <span class="banner-status" id="ec-status"></span>
                    <div class="seg" role="group" aria-label="View">
                        <button type="button" data-view="edit">Edit</button><button type="button" data-view="split" class="on">Split</button><button type="button" data-view="preview">Preview</button>
                    </div>
                    <button class="btn btn-outline btn-sm" id="ec-load-sample" title="Load built-in example">Sample</button>
                    <button class="btn btn-primary btn-sm" id="ec-download">Download .eml</button>
                </div>
            </div>

            <div class="toolbar">
                <div class="toolbar-group">
                    <button class="btn btn-outline btn-sm" data-action="h1">H1</button>
                    <button class="btn btn-outline btn-sm" data-action="h2">H2</button>
                    <button class="btn btn-outline btn-sm" data-action="h3">H3</button>
                    <button class="btn btn-outline btn-sm" data-action="bold" title="Bold"><b>B</b></button>
                    <button class="btn btn-outline btn-sm" data-action="italic" title="Italic"><i>I</i></button>
                    <button class="btn btn-outline btn-sm" data-action="pill">Pill</button>
                </div>
                <div class="toolbar-sep"></div>
                <div class="toolbar-group">
                    <button class="btn btn-outline btn-sm" data-action="quote">&gt; Quote</button>
                    <button class="btn btn-outline btn-sm" data-action="note">+ Note</button>
                </div>
                <div class="toolbar-sep"></div>
                <div class="toolbar-group">
                    <button class="btn btn-outline btn-sm" data-action="bullet">• List</button>
                    <button class="btn btn-outline btn-sm" data-action="numbered">1. List</button>
                    <button class="btn btn-outline btn-sm" data-action="table">Table</button>
                    <button class="btn btn-outline btn-sm" data-action="divider">Divider</button>
                    <button class="btn btn-outline btn-sm" data-action="link">Link</button>
                </div>
                <div class="toolbar-sep"></div>
                <div class="toolbar-group">
                    <button class="btn btn-outline btn-sm" data-action="logo">+ Logo</button>
                    <button class="btn btn-outline btn-sm" data-action="button">+ CTA</button>
                    <button class="btn btn-outline btn-sm" data-action="footer">+ Footer</button>
                    <button class="btn btn-outline btn-sm" data-action="calendar">+ Calendar</button>
                    <button class="btn btn-outline btn-sm" data-action="signature">+ Signature</button>
                </div>
            </div>

            <div class="split" id="ec-split" data-view="split">
                <textarea id="ec-markup" class="editor-textarea" spellcheck="false" placeholder="Write your email in markup — or press Sample to see every component."></textarea>
                <iframe id="ec-preview" class="preview-frame" title="Email preview"></iframe>
            </div>
        </div>
    `;

    // ------------------------------------------------------------------
    // PARSER CONSTANTS & LOGIC
    // ------------------------------------------------------------------
    const EY_YELLOW = '#FFE600';
    const EY_DARK = '#2e2e38';
    const EY_TEXT = '#2E2E38';
    const EY_MUTED = '#6F6F76';
    const EY_FAINT = '#9A9AA0';
    const EY_BORDER = '#E2E2E5';
    const EY_NOTE_BG = '#FFF9D9';
    const EY_QUOTE_BG = '#FAFAFA';
    const FONT = 'Arial, Helvetica, sans-serif';

    const EY_LOGO_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 393 402" width="34" height="35" fill="#fff"><polygon points="267.91,202.77 234.19,267.54 200.56,202.77 134.74,202.77 204.09,322.86 204.09,401.26 263.44,401.26 263.44,322.86 332.89,202.77"></polygon><polygon fill="#ffeb0a" points="392.42,0 0,143.22 392.42,73.9"></polygon><polygon points="3.43,401.26 162.23,401.26 162.23,355.61 62.96,355.61 62.96,322.86 134.74,322.86 134.74,281.18 62.96,281.18 62.96,248.42 142.37,248.42 116.02,202.77 3.43,202.77"></polygon></svg>';

    function escapeHtml(str) {
        return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
    function safeUrl(url) {
        const trimmed = (url || '').trim();
        if (/^javascript:/i.test(trimmed)) return '#';
        return trimmed || '#';
    }

    const PILL_VARIANTS = {
        yellow: { bg: EY_YELLOW, color: EY_DARK },
        dark: { bg: EY_DARK, color: EY_YELLOW },
        grey: { bg: '#E2E2E5', color: EY_DARK },
        gray: { bg: '#E2E2E5', color: EY_DARK }
    };

    function renderPillImage(label, variant) {
        const v = PILL_VARIANTS[variant] || PILL_VARIANTS.yellow;
        const scale = 4, padX = 6, height = 16, radius = height / 2;
        const canvas = document.createElement('canvas');
        const ctx = canvas.getContext('2d');
        ctx.font = '700 11px Arial, Helvetica, sans-serif';
        const width = Math.ceil(ctx.measureText(label).width) + padX * 2;
        canvas.width = width * scale; canvas.height = height * scale;
        ctx.scale(scale, scale);
        ctx.fillStyle = v.bg;
        ctx.beginPath(); ctx.moveTo(radius, 0); ctx.arcTo(width, 0, width, height, radius); ctx.arcTo(width, height, 0, height, radius); ctx.arcTo(0, height, 0, 0, radius); ctx.arcTo(0, 0, width, 0, radius); ctx.closePath(); ctx.fill();
        ctx.fillStyle = v.color; ctx.font = '700 11px Arial, Helvetica, sans-serif'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
        ctx.fillText(label, width / 2, height / 2 + 1);
        return { dataUrl: canvas.toDataURL('image/png'), width, height };
    }

    function renderPillImg(label, variant) {
        if (!label) return '';
        const r = renderPillImage(label, variant);
        return `<img src="${r.dataUrl}" width="${r.width}" height="${r.height}" alt="${escapeHtml(label)}" align="absmiddle" style="display:inline-block;vertical-align:middle;border:0;outline:none;">`;
    }

    function parseInline(raw) {
        const pillTokens = [];
        const withoutPills = raw.replace(/\{\{\s*pill\s*:\s*([^{}|]+?)\s*(?:\|\s*([a-zA-Z]+)\s*)?\}\}/gi, (_, label, variant) => {
            const token = '\u0000PILL' + pillTokens.length + '\u0000';
            pillTokens.push(renderPillImg(label.trim(), (variant || '').trim().toLowerCase()));
            return token;
        });
        let s = escapeHtml(withoutPills);
        s = s.replace(/\[([^\[\]]+)\]\(([^()\s]+)\)/g, (_, text, url) => `<a href="${safeUrl(url)}" style="color:${EY_DARK};text-decoration:underline;font-weight:600;">${text}</a>`);
        s = s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
        s = s.replace(/\*(.+?)\*/g, '<em>$1</em>');
        s = s.replace(/\u0000PILL(\d+)\u0000/g, (_, idx) => pillTokens[parseInt(idx, 10)]);
        return s;
    }

    function svgToPngDataUrl(svgMarkup, canvasW, canvasH) {
        return new Promise((resolve) => {
            const svgBlob = new Blob([svgMarkup], { type: 'image/svg+xml;charset=utf-8' });
            const url = URL.createObjectURL(svgBlob);
            const img = new Image();
            img.onload = () => {
                const canvas = document.createElement('canvas');
                canvas.width = canvasW; canvas.height = canvasH;
                const ctx = canvas.getContext('2d');
                ctx.drawImage(img, 0, 0, canvasW, canvasH);
                URL.revokeObjectURL(url);
                resolve(canvas.toDataURL('image/png'));
            };
            img.onerror = () => { URL.revokeObjectURL(url); resolve(''); };
            img.src = url;
        });
    }

    let logoPngDataUrl = '';
    let logoPngPromise = null;
    function ensureLogoPng() {
        if (!logoPngPromise) {
            logoPngPromise = svgToPngDataUrl(EY_LOGO_SVG, 136, 140).then(url => { logoPngDataUrl = url; return url; });
        }
        return logoPngPromise;
    }

    function presTable(inner, style) {
        return `<table role="presentation" border="0" cellpadding="0" cellspacing="0" style="border-collapse:collapse;mso-table-lspace:0pt;mso-table-rspace:0pt;${style || ''}">${inner}</table>`;
    }

    function renderLogo(title) {
        const imgTag = logoPngDataUrl ? `<img src="${logoPngDataUrl}" width="34" height="35" alt="EY" style="display:block;border:0;outline:none;">` : EY_LOGO_SVG;
        const titleCell = title ? `<td style="padding-left:16px;vertical-align:middle;font-family:${FONT};font-size:16px;font-weight:700;color:#FFFFFF;">${escapeHtml(title)}</td>` : '';
        const inner = presTable(`<tr><td style="vertical-align:middle;">${imgTag}</td>${titleCell}</tr>`);
        const cell = `<td style="background-color:${EY_DARK};border-left-width:6px;border-left-style:solid;border-left-color:${EY_YELLOW};padding:22px 26px;" bgcolor="${EY_DARK}">${inner}</td>`;
        return presTable(`<tr>${cell}</tr>`, 'width:100%;');
    }

    function renderHeading(level, text) {
        if (level === 1) return `<h1 style="margin:0;font-family:${FONT};font-size:25px;line-height:33px;mso-line-height-rule:exactly;font-weight:700;color:${EY_DARK};">${text}</h1>`;
        if (level === 2) return `<h2 style="margin:28px 0 0;font-family:${FONT};font-size:18px;line-height:24px;mso-line-height-rule:exactly;font-weight:700;color:${EY_DARK};border-bottom-width:2px;border-bottom-style:solid;border-bottom-color:${EY_YELLOW};padding-bottom:7px;">${text}</h2>`;
        return `<div style="margin:22px 0 0;font-family:${FONT};font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:${EY_MUTED};">${text}</div>`;
    }

    function renderParagraph(text) { return `<p style="margin:0;font-family:${FONT};font-size:15px;line-height:25px;mso-line-height-rule:exactly;color:${EY_TEXT};">${text}</p>`; }
    function renderQuote(text) { return presTable(`<tr><td style="background-color:${EY_QUOTE_BG};border-left-width:3px;border-left-style:solid;border-left-color:${EY_YELLOW};padding:12px 18px;font-family:${FONT};font-size:14px;line-height:22px;mso-line-height-rule:exactly;color:${EY_MUTED};font-style:italic;" bgcolor="${EY_QUOTE_BG}">${text}</td></tr>`, 'width:100%;'); }
    function renderNote(text) { return presTable(`<tr><td style="background-color:${EY_NOTE_BG};border-left-width:4px;border-left-style:solid;border-left-color:${EY_YELLOW};padding:14px 16px;font-family:${FONT};font-size:14px;line-height:22px;mso-line-height-rule:exactly;color:${EY_DARK};" bgcolor="${EY_NOTE_BG}">${text}</td></tr>`, 'width:100%;'); }

    function renderBulletMarkerImg() {
        const canvas = document.createElement('canvas'); canvas.width = 32; canvas.height = 32;
        const ctx = canvas.getContext('2d'); ctx.scale(4, 4); ctx.fillStyle = EY_YELLOW; ctx.fillRect(0, 0, 8, 8);
        return `<img src="${canvas.toDataURL()}" width="8" height="8" alt="" align="absmiddle" style="display:inline-block;vertical-align:middle;border:0;outline:none;">`;
    }

    function renderNumberBadgeImg(n) {
        const canvas = document.createElement('canvas'); canvas.width = 88; canvas.height = 88;
        const ctx = canvas.getContext('2d'); ctx.scale(4, 4); ctx.fillStyle = EY_DARK; ctx.fillRect(0, 0, 22, 22);
        ctx.fillStyle = EY_YELLOW; ctx.font = '700 12px Arial'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.fillText(String(n), 11, 12);
        return `<img src="${canvas.toDataURL()}" width="22" height="22" alt="${n}." align="absmiddle" style="display:inline-block;vertical-align:middle;border:0;outline:none;">`;
    }

    function renderList(items, ordered) {
        const rows = items.map((item, idx) => {
            const padBottom = idx === items.length - 1 ? '0' : '10px';
            const marker = ordered ? renderNumberBadgeImg(idx + 1) : renderBulletMarkerImg();
            return `<tr><td valign="top" style="vertical-align:top;font-family:${FONT};font-size:15px;line-height:24px;mso-line-height-rule:exactly;color:${EY_TEXT};padding:0 0 ${padBottom} 30px;text-indent:-30px;">${marker}&nbsp;&nbsp;${item}</td></tr>`;
        }).join('');
        return presTable(rows, 'width:100%;');
    }

    function renderDivider() { return presTable(`<tr><td align="center" style="padding:0;">${presTable('<tr><td style="font-size:0;line-height:0;border-top-width:3px;border-top-style:solid;border-top-color:#D5D5DA;">&nbsp;</td></tr>', 'width:90%;margin:0 auto;')}</td></tr>`, 'width:100%;'); }

    function renderButton(label, url) {
        const link = `<a href="${safeUrl(url)}" target="_blank" style="display:inline-block;padding:14px 32px;font-family:${FONT};font-size:14px;font-weight:700;color:${EY_DARK};background-color:${EY_YELLOW};text-decoration:none;text-align:center;letter-spacing:.4px;text-transform:uppercase;">${escapeHtml(label || 'Click Here')}</a>`;
        return presTable(`<tr><td align="center" bgcolor="${EY_YELLOW}" style="background-color:${EY_YELLOW};">${link}</td></tr>`) + '<div style="clear:both;line-height:0;font-size:0;">&nbsp;</div>';
    }

    function renderTable(content) {
        const lines = content.split('\n').map(r => r.trim()).filter(Boolean);
        if (!lines.length) return '';
        const cells = lines.map(r => r.split('|').map(c => c.trim()));
        let rows = '<tr>' + cells[0].map(h => `<td valign="middle" style="background-color:${EY_DARK};color:${EY_YELLOW};font-family:${FONT};font-size:12px;font-weight:700;text-transform:uppercase;padding:11px 14px;border-bottom:2px solid ${EY_YELLOW};">${parseInline(h)}</td>`).join('') + '</tr>';
        cells.slice(1).forEach((row, i) => {
            const bg = i % 2 === 0 ? '#FFFFFF' : '#F6F6F7';
            rows += '<tr>' + row.map(c => `<td valign="middle" style="background-color:${bg};color:${EY_TEXT};font-family:${FONT};font-size:14px;padding:10px 14px;border-bottom:1px solid ${EY_BORDER};">${parseInline(c)}</td>`).join('') + '</tr>';
        });
        return presTable(rows, 'width:100%;');
    }

    function renderFooter(text) { return `<div style="margin:0;padding-top:18px;border-top:1px solid ${EY_BORDER};font-family:${FONT};font-size:11px;line-height:18px;color:${EY_FAINT};">${text}</div>`; }
    function renderSignature(spec) {
        const parts = (spec || '').split('|').map(s => s.trim());
        return presTable(`<tr><td style="padding-top:18px;border-top:1px solid ${EY_BORDER};"><div style="font-family:${FONT};font-size:13px;font-weight:700;color:${EY_DARK};">${escapeHtml(parts[0]||'')} | ${escapeHtml(parts[1]||'')} | ${escapeHtml(parts[2]||'')}</div><div style="font-family:${FONT};font-size:12px;color:${EY_TEXT};padding-top:2px;">${escapeHtml(parts[3]||'')}</div><div style="font-family:${FONT};font-size:12px;color:${EY_MUTED};padding-top:2px;">Mobile: ${escapeHtml(parts[4]||'')} | ${escapeHtml(parts[5]||'')}</div></td></tr>`, 'width:100%;');
    }

    function renderDayBadgeImg(day) {
        const canvas = document.createElement('canvas'); canvas.width = 104; canvas.height = 104;
        const ctx = canvas.getContext('2d'); ctx.scale(4, 4); ctx.fillStyle = EY_YELLOW; ctx.beginPath(); ctx.arc(13, 13, 13, 0, Math.PI * 2); ctx.fill();
        ctx.fillStyle = EY_DARK; ctx.font = '700 13px Arial'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.fillText(String(day), 13, 14);
        return `<img src="${canvas.toDataURL()}" width="26" height="26" alt="${day}" style="display:block;margin:0 auto;line-height:26px;border:0;">`;
    }

    function renderCalendar(spec) {
        const parts = spec.split('|').map(s => s.trim());
        const ymMatch = parts[0].match(/^(\d{4})-(\d{1,2})$/);
        if (!ymMatch) return renderNote('Calendar: expected format YYYY-MM');
        const year = parseInt(ymMatch[1], 10), month = parseInt(ymMatch[2], 10);
        const daysInMonth = new Date(year, month, 0).getDate();
        const highlightSet = new Set((parts[1] || '').split(',').map(s => parseInt(s.trim(), 10)));
        const firstWeekday = (new Date(year, month - 1, 1).getDay() + 6) % 7;

        let weeks = [], week = new Array(firstWeekday).fill(null);
        for (let d = 1; d <= daysInMonth; d++) { week.push(d); if (week.length === 7) { weeks.push(week); week = []; } }
        if (week.length) { while (week.length < 7) week.push(null); weeks.push(week); }

        const titleRow = `<tr><td colspan="7" style="background-color:${EY_DARK};padding:8px 10px;text-align:center;font-family:${FONT};font-size:13px;font-weight:700;color:#FFFFFF;border-bottom:3px solid ${EY_YELLOW};">${['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][month-1]} ${year}</td></tr>`;
        const headerRow = '<tr>' + ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'].map(l => `<td width="56" align="center" style="background-color:${EY_BORDER};color:${EY_MUTED};font-family:${FONT};font-size:10px;font-weight:700;text-transform:uppercase;padding:5px 2px;border:1px solid ${EY_BORDER};">${l}</td>`).join('') + '</tr>';
        const bodyRows = weeks.map(wr => '<tr>' + wr.map((day, i) => {
            if (!day) return `<td width="56" height="34" style="background-color:#FFFFFF;border:1px solid ${EY_BORDER};">&nbsp;</td>`;
            const bg = (i===5||i===6) ? '#D9D9DC' : '#F4F4F5';
            const hl = highlightSet.has(day);
            return `<td width="56" align="center" valign="middle" height="34" style="background-color:${bg};font-family:${FONT};${hl?'':'font-size:13px;'}color:#000000;border:1px solid ${EY_BORDER};">${hl ? renderDayBadgeImg(day) : day}</td>`;
        }).join('') + '</tr>').join('');
        return presTable(`<tr><td align="center">${presTable(titleRow + headerRow + bodyRows, 'width:392px;')}</td></tr>`, 'width:100%;');
    }

    function renderBlock(block) {
        const lines = block.split('\n').map(l => l.trim());
        const first = lines[0]; let m;
        if (m = block.match(/^\[\[\s*logo\s*(?::([\s\S]*))?\]\]\s*$/i)) return renderLogo(m[1] ? m[1].trim() : '');
        if (m = block.match(/^\[\[\s*table\s*\]\]\n([\s\S]*?)\n\[\[\/table\s*\]\]\s*$/i)) return renderTable(m[1]);
        if (m = block.match(/^\[\[\s*button\s*:([\s\S]*)\]\]\s*$/i)) { const p = m[1].split('|').map(s=>s.trim()); return renderButton(p[0], p[1]); }
        if (m = block.match(/^\[\[\s*note\s*:([\s\S]*)\]\]\s*$/i)) return renderNote(parseInline(m[1].trim()));
        if (m = block.match(/^\[\[\s*footer\s*:([\s\S]*)\]\]\s*$/i)) return renderFooter(parseInline(m[1].trim()));
        if (m = block.match(/^\[\[\s*calendar\s*:([\s\S]*)\]\]\s*$/i)) return renderCalendar(m[1].trim());
        
        // FIX: Removed the strict mode syntax error here
        if (m = block.match(/^\[\[\s*signature\s*(?::([\s\S]*))?\]\]\s*$/i)) return renderSignature(m[1] ? m[1].trim() : '');
        
        if (lines.length === 1 && /^-{3,}$/.test(first)) return renderDivider();
        if (m = first.match(/^(#{1,3})\s+(.*)$/)) return renderHeading(m[1].length, parseInline(m[2]));
        if (lines.every(l => /^>\s?/.test(l))) return renderQuote(parseInline(lines.map(l => l.replace(/^>\s?/, '')).join(' ')));
        if (lines.every(l => /^[-*]\s+/.test(l))) return renderList(lines.map(l => parseInline(l.replace(/^[-*]\s+/, ''))), false);
        if (lines.every(l => /^\d+\.\s+/.test(l))) return renderList(lines.map(l => parseInline(l.replace(/^\d+\.\s+/, ''))), true);
        return renderParagraph(parseInline(lines.join(' ')));
    }

    function parseMarkup(src) {
        const lines = (src || '').replace(/\r\n/g, '\n').split('\n');
        const segments = []; let i = 0;
        while (i < lines.length) {
            if (lines[i].trim() === '') {
                let count = 0; while (i < lines.length && lines[i].trim() === '') { count++; i++; }
                segments.push({ type: 'blank', count });
            } else {
                const block = []; while (i < lines.length && lines[i].trim() !== '') { block.push(lines[i]); i++; }
                segments.push({ type: 'content', text: block.join('\n') });
            }
        }
        while (segments.length && segments[0].type === 'blank') segments.shift();
        while (segments.length && segments[segments.length - 1].type === 'blank') segments.pop();

        if (!segments.length) return { logo: '', content: `<p style="text-align:center;color:${EY_FAINT};padding:40px 0;font-family:${FONT};">Start typing...</p>` };

        let logoHtml = '', logoUsed = false;
        const renderedBlocks = [], gapAfter = [];
        segments.forEach(seg => {
            if (seg.type === 'blank') { if (renderedBlocks.length) gapAfter[renderedBlocks.length - 1] = seg.count; return; }
            const match = !logoUsed && seg.text.match(/^\[\[\s*logo\s*(?::([\s\S]*))?\]\]\s*$/i);
            if (match) { logoHtml = renderLogo(match[1] ? match[1].trim() : ''); logoUsed = true; } 
            else renderedBlocks.push(renderBlock(seg.text));
        });

        let content = '';
        renderedBlocks.forEach((html, idx) => {
            content += html;
            if (idx < renderedBlocks.length - 1) {
                content += presTable(`<tr><td height="16" style="font-size:0;line-height:16px;">&nbsp;</td></tr>`, 'width:100%;');
                const extra = (gapAfter[idx] || 1) - 1;
                if (extra > 0) content += `<div style="line-height:25px;">${'<br>'.repeat(extra)}</div>`;
            }
        });
        return { logo: logoHtml, content };
    }

    function buildStandaloneDoc(subject, parsed) {
        const logoRow = parsed.logo ? `<tr><td style="padding:0;">${parsed.logo}</td></tr>` : '';
        const card = `<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background-color:#FFFFFF;border-collapse:collapse;" bgcolor="#FFFFFF">${logoRow}<tr><td style="padding:36px 40px 32px;">${parsed.content}</td></tr></table>`;
        return `<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><title>${escapeHtml(subject || 'Email')}</title></head><body style="margin:0;padding:0;background-color:#ECECEE;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#ECECEE;padding:32px 16px;font-family:${FONT};border-collapse:collapse;"><tr><td align="center">${card}</td></tr></table></body></html>`;
    }

    // ------------------------------------------------------------------
    // EDITOR LOGIC
    // ------------------------------------------------------------------
    const $ = s => root.querySelector(s);
    const els = {
        markup: $('#ec-markup'),
        preview: $('#ec-preview'),
        title: $('#ec-title'),
        status: $('#ec-status'),
        split: $('#ec-split'),
        btnSample: $('#ec-load-sample'),
        btnDownload: $('#ec-download')
    };

    // RESTORED: Exact original example markup
    const EXAMPLE_MARKUP = [
        '[[logo: Email Composer — Component Reference]]\n\n',
        '# Email Composer\n\n',
        'This tool lets you write structured professional emails using a simple markup language. The preview on the right updates live as you type. Every component below is available from the toolbar or by typing the syntax directly.\n\n',
        '## Text formatting\n\n',
        'Use **bold** to emphasise key terms and *italic* for secondary notes. Inline [links](https://example.com) are supported with standard markdown syntax. You can also place status labels inline using pills — for example: project is {{pill:In progress|dark}}, review is {{pill:Pending}} or deliverable is {{pill:Done|grey}}.\n\n',
        '## Headings\n\n',
        'Three heading levels are available. **# H1** is used for the email title at the top. **## H2** creates section headings with a yellow underline — like the ones on this page. **### H3** renders as a small uppercase label.\n\n',
        '### This is an H3 label\n\n',
        'Use it to introduce a subsection without the visual weight of an H2.\n\n',
        '## Lists\n\n',
        'Bullet lists use a **-** prefix. Each item gets a solid yellow square marker:\n\n',
        '- Compose emails using a lightweight markup language\n',
        '- Preview renders live alongside the editor {{pill:Live}}\n',
        '- Download a ready-to-send **.eml** file at any time\n\n',
        'Numbered lists use **1.** prefix and render with a dark badge:\n\n',
        '1. Type your markup in the left panel\n',
        '2. Review the formatted email in the right panel\n',
        '3. Click **Download .eml** to export\n\n',
        '## Data tables\n\n',
        '[[table]]\n',
        'Component | Syntax | Notes\n',
        'Heading | # / ## / ### | Three levels\n',
        'Bold | **text** | Inline\n',
        'Italic | *text* | Inline\n',
        'Bullet list | - item | Consecutive lines\n',
        'Numbered list | 1. item | Consecutive lines\n',
        'Data table | [[table]] … [[/table]] | Pipe-separated columns\n',
        'Divider | --- | Horizontal rule\n',
        'Link | [label](url) | Inline\n',
        '[[/table]]\n\n',
        '## Callouts\n\n',
        '[[note: Use a note to highlight something the reader must not overlook — a deadline, a caveat, or a key instruction.]]\n\n',
        '> Use a quote for a secondary disclaimer, a cited figure, or a statement that needs visual separation from the main copy.\n\n',
        '## Call to action\n\n',
        '[[button: Open Documentation | https://example.com]]\n\n',
        '## Calendar\n\n',
        'Use [[calendar: YYYY-MM]] to print a month, or add a pipe-separated list of days to ring in yellow: [[calendar: YYYY-MM | 1,4,9]].\n\n',
        '[[calendar: 2026-07 | 1,2,3,9]]\n\n',
        '---\n\n',
        'The **[[logo]]** tag at the top inserts the EY mark on a full-width dark header bar with a yellow accent line. You can optionally add a title: **[[logo: Your Title Here]]**. The **[[footer: text]]** tag at the very end adds a small disclaimer block — edit or delete it as needed.\n\n',
        '[[footer: This message (including any attachments) is confidential and may be privileged. If you are not the intended recipient, please notify the sender immediately and delete this message. EY refers to the global organization and may refer to one or more of the member firms of Ernst & Young Global Limited, each of which is a separate legal entity.]]\n\n',
        '[[signature: Name | Rank | Dept | Company | Cell | Email]]'
    ].join('');

    // Draft is kept per device, in the same store the Notes module uses
    const DRAFT_KEY = 'email_composer_draft';
    let saveTimer = null, statusTimer = null;
    function scheduleSave() {
        clearTimeout(saveTimer);
        els.status.textContent = 'Saving…';
        saveTimer = setTimeout(async () => {
            await App.Store.set(DRAFT_KEY, els.markup.value);
            els.status.textContent = 'Draft saved';
            clearTimeout(statusTimer);
            statusTimer = setTimeout(() => { els.status.textContent = ''; }, 2000);
        }, 800);
    }

    function extractSubject(src) {
        const m = (src || '').match(/^#\s+(.+)$/m);
        return m ? m[1].replace(/\*\*/g, '').replace(/\*/g, '').trim() : '';
    }

    function updatePreview() {
        const subject = extractSubject(els.markup.value);
        els.preview.srcdoc = buildStandaloneDoc(subject, parseMarkup(els.markup.value));
        els.title.textContent = subject || 'Untitled email';
        els.title.classList.toggle('placeholder', !subject);
    }
    const onChange = () => { updatePreview(); scheduleSave(); };

    els.markup.addEventListener('input', onChange);

    // Toolbar logic
    function withPreservedScroll(mutate) {
        const scrollTop = els.markup.scrollTop;
        mutate();
        els.markup.focus();
        els.markup.scrollTop = scrollTop;
        onChange();
    }

    function insertText(prefix, suffix = '') {
        withPreservedScroll(() => {
            const start = els.markup.selectionStart, end = els.markup.selectionEnd;
            const selected = els.markup.value.slice(start, end);
            els.markup.value = els.markup.value.slice(0, start) + prefix + selected + suffix + els.markup.value.slice(end);
            els.markup.selectionStart = start + prefix.length;
            els.markup.selectionEnd = els.markup.selectionStart + selected.length;
        });
    }

    function insertBlock(snippet) {
        withPreservedScroll(() => {
            const pos = els.markup.selectionStart;
            const val = els.markup.value;
            const before = val.slice(0, pos), after = val.slice(pos);
            const lead = before === '' || before.endsWith('\n\n') ? '' : before.endsWith('\n') ? '\n' : '\n\n';
            const trail = after === '' || after.startsWith('\n\n') ? '' : after.startsWith('\n') ? '\n' : '\n\n';
            els.markup.value = before + lead + snippet + trail + after;
            els.markup.selectionStart = els.markup.selectionEnd = (before + lead + snippet).length;
        });
    }

    const ACTIONS = {
        h1: () => insertText('# ', ''), h2: () => insertText('## ', ''), h3: () => insertText('### ', ''),
        bold: () => insertText('**', '**'), italic: () => insertText('*', '*'), pill: () => insertText('{{pill:', '}}'),
        quote: () => insertText('> ', ''), bullet: () => insertText('- ', ''), numbered: () => insertText('1. ', ''),
        table: () => insertBlock('[[table]]\nCol A | Col B\nVal 1 | Val 2\n[[/table]]'),
        divider: () => insertBlock('---'), link: () => insertBlock('[Label](https://url)'),
        logo: () => insertBlock('[[logo: Title]]'), button: () => insertBlock('[[button: Label | https://url]]'),
        note: () => insertBlock('[[note: Text]]'), footer: () => insertBlock('[[footer: Text]]'),
        calendar: () => insertBlock('[[calendar: 2026-01]]'), signature: () => insertBlock('[[signature: Name | Rank | Dept | Company | Cell | Email]]')
    };

    root.addEventListener('click', e => {
        const btn = e.target.closest('[data-action]');
        if (btn && ACTIONS[btn.dataset.action]) ACTIONS[btn.dataset.action]();
    });

    // Edit | Split | Preview — phones start on Edit, wide screens on Split
    function setView(view) {
        els.split.dataset.view = view;
        root.querySelectorAll('.seg [data-view]').forEach(b => b.classList.toggle('on', b.dataset.view === view));
    }
    root.querySelectorAll('.seg [data-view]').forEach(b => { b.onclick = () => setView(b.dataset.view); });
    if (window.matchMedia?.('(max-width: 768px)').matches) setView('edit');

    els.btnSample.onclick = () => {
        if (els.markup.value.trim() && !confirm("Replace current draft?")) return;
        els.markup.value = EXAMPLE_MARKUP;
        onChange();
    };

    els.btnDownload.onclick = async () => {
        await ensureLogoPng();
        const subject = extractSubject(els.markup.value) || 'Email';
        const html = buildStandaloneDoc(subject, parseMarkup(els.markup.value));
        const eml = `From: user@example.com\r\nSubject: ${subject}\r\nMIME-Version: 1.0\r\nContent-Type: text/html; charset="UTF-8"\r\nContent-Transfer-Encoding: 8bit\r\n\r\n${html}`;
        const blob = new Blob([eml], { type: 'message/rfc822' });
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = (subject.replace(/[^\w\- ]+/g, '').trim() || 'email') + '.eml';
        a.click();
        URL.revokeObjectURL(a.href);
    };

    // Load the draft (older versions kept it in localStorage — pick that up once)
    let saved = await App.Store.get(DRAFT_KEY);
    if (saved === undefined || saved === null) saved = localStorage.getItem(DRAFT_KEY);
    els.markup.value = saved || EXAMPLE_MARKUP;
    updatePreview();                           // show something immediately…
    ensureLogoPng().then(updatePreview);       // …and again once the logo PNG is ready
}
