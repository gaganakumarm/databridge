import { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './style.css';

function App() {
  const [connection, setConnection] = useState('Checking API connection?');
  useEffect(() => {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 5000);
    void fetch('/health', { signal: controller.signal })
      .then(async response => {
        if (!response.ok) throw new Error('Unavailable');
        const data: unknown = await response.json();
        if (typeof data !== 'object' || data === null || !('status' in data) || data.status !== 'ok') throw new Error('Invalid response');
        setConnection('API connected');
      })
      .catch(() => setConnection('API unavailable ? start the backend and refresh'))
      .finally(() => window.clearTimeout(timeout));
    return () => { window.clearTimeout(timeout); controller.abort(); };
  }, []);
  return <main><nav aria-label="Brand"><span className="mark">D</span> DataBridge <span className="badge">SPRINT 0</span></nav>
    <section><p className="eyebrow">A FOUNDATION FOR YOUR DATA</p><h1>Connect with confidence.</h1>
    <p className="intro">A clear path from source to destination. We?re building a secure, reliable home for your data integrations.</p>
    <div className="connection" role="status"><span aria-hidden="true">?</span> {connection}</div></section>
    <div className="features"><article><span>01</span><h2>Purposeful architecture</h2><p>A modular foundation that stays simple as it grows.</p></article>
    <article><span>02</span><h2>Visibility built in</h2><p>Structured request logs and correlation for easier diagnosis.</p></article>
    <article><span>03</span><h2>Quality from day one</h2><p>Automated checks support every change.</p></article></div>
    <footer>DataBridge ? Development foundation <span>Integration features arrive in later sprints.</span></footer></main>;
}
createRoot(document.getElementById('root')!).render(<App />);
