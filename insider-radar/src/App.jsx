import { useState, useEffect } from 'react';
import './App.css';

function App() {
  const [selectedTrade, setSelectedTrade] = useState(null);
  const [trades, setTrades] = useState([]);
  const [error, setError] = useState(null);

  useEffect(() => {
    // Функция для загрузки "живых" сделок
    const fetchTrades = () => {
      fetch('/trades_data.json?t=' + new Date().getTime()) // Избегаем кэша
        .then(res => {
          if (!res.ok) throw new Error('Не удалось загрузить данные из JSON');
          return res.json();
        })
        .then(data => {
          setTrades(data);
          setError(null);
        })
        .catch(err => {
          console.error(err);
          setError('Ошибка связи с базой данных сделок.');
        });
    };

    fetchTrades();
    // Обновляем каждые 5 секунд
    const interval = setInterval(fetchTrades, 5000);
    return () => clearInterval(interval);
  }, []);

  return (
    <div className="dashboard-container">
      <header className="dashboard-header">
        <h1>InsiderRadar</h1>
        <p>Радар "умных денег" и крупных сделок в реальном времени</p>
      </header>

      <main className="dashboard-main">
        {error && <div className="trade-tag" style={{background: 'rgba(239, 68, 68, 0.2)', color: '#ef4444', marginBottom: '1rem', border: '1px solid currentColor', textAlign:'center', padding: '10px'}}>{error}</div>}
        
        <div className="trades-list">
          <h2>Последние подозрительные сделки</h2>
          {trades.length === 0 && !error && <p>🔍 Сканируем рынок...</p>}
          
          {trades.map((trade, index) => (
            <div 
              key={trade.id || index} 
              className={`trade-card ${selectedTrade === (trade.id || index) ? 'active' : ''}`}
              onClick={() => setSelectedTrade(selectedTrade === (trade.id || index) ? null : (trade.id || index))}
            >
              <div className="trade-card-header">
                <div className="trade-ticker" style={{ borderColor: trade.color }}>
                  {trade.ticker}
                </div>
                <div className="trade-premium" style={{ color: trade.color }}>
                  {trade.premium}
                </div>
              </div>
              
              <div className="trade-details">
                <span className="trade-type">{trade.type}</span>
                <span className="trade-strike">Страйк: {trade.strike}</span>
                <span className="trade-expiry">до {trade.expiry}</span>
                <span className="trade-tag">{trade.tag}</span>
              </div>

              <div className={`trade-explanation ${selectedTrade === (trade.id || index) ? 'open' : ''}`}>
                <div className="explanation-title">💡 Объяснение для чайника:</div>
                <div className="explanation-text">{trade.simpleExplanation}</div>
              </div>
            </div>
          ))}
        </div>
      </main>
    </div>
  );
}

export default App;
