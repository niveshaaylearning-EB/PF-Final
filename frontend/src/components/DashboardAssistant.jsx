import { useState, useRef, useEffect } from 'react';
import axios from 'axios';
import { MessageCircle, X, Send, Loader2, Download } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { getToken, isLoggedIn, getEmail } from '../utils/auth.js';
import { API_BASE } from '../config.js';

// Assistant replies come back as markdown (tables, bold, bullet lists --
// e.g. the "Caprize's early buys" multi-tenure comparison table), which was
// previously dumped as raw pipe-table text (whiteSpace: pre-wrap only, no
// parsing) -- confirmed unreadable live, 2026-11-21. react-markdown + GFM
// renders it properly; only the per-element inline styles below are needed
// since this widget doesn't load a global markdown stylesheet.
const MARKDOWN_COMPONENTS = {
  p: ({ node, ...props }) => <p style={{ margin: '0 0 6px', lineHeight: 1.5 }} {...props} />,
  strong: ({ node, ...props }) => <strong style={{ fontWeight: 700 }} {...props} />,
  ul: ({ node, ...props }) => <ul style={{ margin: '0 0 6px', paddingLeft: '18px' }} {...props} />,
  ol: ({ node, ...props }) => <ol style={{ margin: '0 0 6px', paddingLeft: '18px' }} {...props} />,
  li: ({ node, ...props }) => <li style={{ marginBottom: '2px' }} {...props} />,
  table: ({ node, ...props }) => (
    <div style={{ overflowX: 'auto', margin: '4px 0 8px' }}>
      <table style={{ borderCollapse: 'collapse', width: '100%', fontSize: '0.78rem' }} {...props} />
    </div>
  ),
  thead: ({ node, ...props }) => <thead style={{ background: 'rgba(99,102,241,0.12)' }} {...props} />,
  th: ({ node, ...props }) => <th style={{ border: '1px solid var(--panel-border)', padding: '4px 8px', textAlign: 'left', fontWeight: 700 }} {...props} />,
  td: ({ node, ...props }) => <td style={{ border: '1px solid var(--panel-border)', padding: '4px 8px' }} {...props} />,
  code: ({ node, ...props }) => <code style={{ background: 'rgba(127,127,127,0.15)', borderRadius: '4px', padding: '1px 4px', fontSize: '0.78rem' }} {...props} />,
  h1: ({ node, ...props }) => <div style={{ fontWeight: 700, fontSize: '0.9rem', margin: '4px 0' }} {...props} />,
  h2: ({ node, ...props }) => <div style={{ fontWeight: 700, fontSize: '0.86rem', margin: '4px 0' }} {...props} />,
  h3: ({ node, ...props }) => <div style={{ fontWeight: 700, fontSize: '0.84rem', margin: '4px 0' }} {...props} />,
};

function exportChatAsMarkdown(messages) {
  const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
  const lines = [
    '# Dashboard Assistant — chat export',
    '',
    `Exported: ${new Date().toLocaleString()}`,
    `User: ${getEmail() || 'unknown'}`,
    '',
    '---',
    '',
  ];
  messages.forEach(m => {
    lines.push(m.role === 'user' ? '## You' : '## Assistant');
    lines.push('');
    lines.push(m.content);
    lines.push('');
  });
  const blob = new Blob([lines.join('\n')], { type: 'text/markdown' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `dashboard-assistant-chat-${stamp}.md`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

// Floating chat widget, mounted once in App.jsx so it's available on every
// page. Opened to every logged-in user (2026-11-21, by request -- was
// admin-only before that); the backend endpoint it talks to (/api/admin/
// assistant/ask) still requires a valid login via JWTMiddleware, same as
// every other /api/* route, but no longer an admin one specifically.

export default function DashboardAssistant() {
  const [open, setOpen] = useState(false);
  const [messages, setMessages] = useState([]); // [{role: 'user'|'assistant', content}]
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const scrollRef = useRef(null);

  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [messages, loading]);

  if (!isLoggedIn()) return null;

  const send = async () => {
    const question = input.trim();
    if (!question || loading) return;
    setInput('');
    setError('');
    const nextMessages = [...messages, { role: 'user', content: question }];
    setMessages(nextMessages);
    setLoading(true);
    try {
      // Only pass prior user/assistant turns as history -- tool-call/tool
      // messages are internal to one request and never need to round-trip
      // back to the frontend.
      const history = messages.map(m => ({ role: m.role, content: m.content }));
      const resp = await axios.post(`${API_BASE}/admin/assistant/ask`, { question, history }, {
        headers: { Authorization: `Bearer ${getToken()}` },
      });
      if (resp.data.error) {
        setError(resp.data.error);
      } else {
        setMessages([...nextMessages, { role: 'assistant', content: resp.data.answer || '(no answer)' }]);
      }
    } catch (err) {
      setError(err.response?.data?.detail || err.message || 'Failed to reach the assistant.');
    } finally {
      setLoading(false);
    }
  };

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  };

  return (
    <div style={{ position: 'fixed', bottom: '24px', right: '24px', zIndex: 999999 }}>
      {open ? (
        <div style={{
          width: '360px', maxWidth: 'calc(100vw - 48px)', height: '480px', maxHeight: 'calc(100vh - 100px)',
          display: 'flex', flexDirection: 'column',
          background: 'var(--select-bg)', border: '1px solid var(--panel-border)', borderRadius: '14px',
          boxShadow: '0 12px 40px rgba(0,0,0,0.35)', overflow: 'hidden',
        }}>
          <div style={{ padding: '12px 16px', display: 'flex', alignItems: 'center', justifyContent: 'space-between', borderBottom: '1px solid var(--panel-border)', background: 'rgba(99,102,241,0.08)' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <MessageCircle size={16} color="var(--primary)" />
              <span style={{ fontSize: '0.85rem', fontWeight: 700, color: 'var(--text-main)' }}>Dashboard Assistant</span>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
              <button onClick={() => exportChatAsMarkdown(messages)} disabled={messages.length === 0}
                title="Export this chat as a Markdown file"
                style={{ background: 'none', border: 'none', color: messages.length === 0 ? 'var(--text-muted)' : 'var(--text-main)', cursor: messages.length === 0 ? 'not-allowed' : 'pointer', display: 'flex', padding: 0, opacity: messages.length === 0 ? 0.4 : 1 }}>
                <Download size={15} />
              </button>
              <button onClick={() => setOpen(false)} style={{ background: 'none', border: 'none', color: 'var(--text-muted)', cursor: 'pointer', display: 'flex', padding: 0 }}>
                <X size={16} />
              </button>
            </div>
          </div>

          <div ref={scrollRef} style={{ flex: 1, overflowY: 'auto', padding: '12px 14px', display: 'flex', flexDirection: 'column', gap: '10px' }}>
            {messages.length === 0 && (
              <div style={{ fontSize: '0.78rem', color: 'var(--text-muted)', textAlign: 'center', marginTop: '20px', lineHeight: 1.6 }}>
                Ask anything about what's in this dashboard -- holdings, rebalance history, result-update status, corporate actions, watchlist, competitor data...
              </div>
            )}
            {messages.map((m, i) => (
              <div key={i} style={{
                alignSelf: m.role === 'user' ? 'flex-end' : 'flex-start',
                maxWidth: '85%', padding: '8px 12px', borderRadius: '10px', fontSize: '0.82rem', lineHeight: 1.5,
                whiteSpace: m.role === 'user' ? 'pre-wrap' : 'normal',
                background: m.role === 'user' ? 'var(--primary)' : 'var(--input-bg)',
                color: m.role === 'user' ? '#fff' : 'var(--text-main)',
                border: m.role === 'user' ? 'none' : '1px solid var(--panel-border)',
              }}>
                {m.role === 'user' ? m.content : (
                  <ReactMarkdown remarkPlugins={[remarkGfm]} components={MARKDOWN_COMPONENTS}>{m.content}</ReactMarkdown>
                )}
              </div>
            ))}
            {loading && (
              <div style={{ alignSelf: 'flex-start', display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.78rem', color: 'var(--text-muted)' }}>
                <Loader2 size={13} style={{ animation: 'spin 1s linear infinite' }} /> Thinking…
              </div>
            )}
            {error && (
              <div style={{ alignSelf: 'flex-start', fontSize: '0.78rem', color: '#f87171', padding: '6px 10px', background: 'rgba(239,68,68,0.1)', borderRadius: '8px', border: '1px solid rgba(239,68,68,0.3)' }}>
                {error}
              </div>
            )}
          </div>

          <div style={{ padding: '10px 12px', borderTop: '1px solid var(--panel-border)', display: 'flex', gap: '8px' }}>
            <textarea
              value={input} onChange={e => setInput(e.target.value)} onKeyDown={handleKeyDown}
              placeholder="Ask a question…" rows={1} disabled={loading}
              style={{ flex: 1, resize: 'none', padding: '8px 10px', borderRadius: '8px', border: '1px solid var(--panel-border)', background: 'var(--input-bg)', color: 'var(--text-main)', fontSize: '0.82rem', fontFamily: 'inherit', outline: 'none' }}
            />
            <button onClick={send} disabled={loading || !input.trim()} style={{
              display: 'flex', alignItems: 'center', justifyContent: 'center', width: '36px', height: '36px', borderRadius: '8px', border: 'none',
              background: (loading || !input.trim()) ? 'var(--input-bg)' : 'var(--primary)', color: (loading || !input.trim()) ? 'var(--text-muted)' : '#fff',
              cursor: (loading || !input.trim()) ? 'not-allowed' : 'pointer',
            }}>
              <Send size={15} />
            </button>
          </div>
        </div>
      ) : (
        <button onClick={() => setOpen(true)} title="Ask the Dashboard Assistant" style={{
          width: '54px', height: '54px', borderRadius: '50%', border: 'none', cursor: 'pointer',
          background: 'var(--primary)', color: '#fff', display: 'flex', alignItems: 'center', justifyContent: 'center',
          boxShadow: '0 6px 20px rgba(99,102,241,0.4)',
        }}>
          <MessageCircle size={24} />
        </button>
      )}
    </div>
  );
}
