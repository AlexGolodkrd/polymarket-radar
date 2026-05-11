# Flask Best Practices

**Source**: microsoft/debugpy/.claude/skills/flask

## Architecture

### App factory pattern
```python
def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)
    
    # init extensions LAZILY
    db.init_app(app)
    
    # register blueprints
    app.register_blueprint(api_bp, url_prefix='/api')
    
    return app
```

**Why**: testability (each test gets fresh app), avoids global state, supports multiple instances.

### Blueprints (modular routing)
Group related routes into `Blueprint` objects, register in factory.

### Config in separate classes
```python
class BaseConfig:
    SECRET_KEY = os.environ['SECRET_KEY']
class DevConfig(BaseConfig): DEBUG = True
class TestConfig(BaseConfig): TESTING = True
class ProdConfig(BaseConfig): pass
```

## Routes

### Use specific HTTP method decorators
```python
@app.get('/api/deals')   # ✅ NOT @app.route('/api/deals', methods=['GET'])
@app.post('/api/scan')   # ✅
```

### Validate input early
```python
@app.post('/api/order')
def create_order():
    data = request.get_json()
    if not data or 'price' not in data:
        abort(400, 'price required')
    # ... rest of handler
```

### Use `flask.abort()` for errors
Don't manually construct `(jsonify({...}), 400)` — use `abort(400, message)`.

## Extensions (lazy init)

```python
db = SQLAlchemy()  # at module level — no app yet
def create_app():
    app = Flask(__name__)
    db.init_app(app)  # bind to app HERE
```

## Testing

```python
@pytest.fixture
def app():
    app = create_app(TestConfig)
    yield app

@pytest.fixture
def client(app):
    return app.test_client()

def test_get_deals(client):
    r = client.get('/api/deals')
    assert r.status_code == 200
```

## CRITICAL warnings

### Never use Flask dev server in production
```python
# ❌ BAD (what plan-kapkan currently does)
app.run(host='0.0.0.0', port=5050, debug=False, threaded=True)

# ✅ GOOD
# Use gunicorn / uwsgi as the WSGI server
# gunicorn -w 4 -b 127.0.0.1:5050 'arb_server:app'
```

### Don't store mutable state on `app`
```python
# ❌ app.scan_data = {...}  
# ✅ Use module-level globals OR Flask's `g` for request scope
```

### Externalize `SECRET_KEY`
```python
SECRET_KEY = os.environ['SECRET_KEY']  # never hardcode
```

## Application to plan-kapkan

Issues we have:
1. ❌ Using Flask dev server in production (`app.run(...)`) — should be gunicorn
2. ❌ No app factory pattern (single global `app = Flask(__name__)`)
3. ❌ Mutable global `scan_data` dict — would break under multiple worker processes
4. ✅ Externalized config via `Credentials.env`
5. ✅ Use `@app.route()` (not method-specific, but acceptable)

**Critical fix to do**: switch from dev server to gunicorn — it'll handle threading + production-grade properly.
