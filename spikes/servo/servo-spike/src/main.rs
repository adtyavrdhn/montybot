//! Minimal headless Servo embedding spike.
//!
//! Usage: servo-spike <url> [--out shot.png] [--form-test] [--two-instances]
//!                          [--experimental] [--width 1280] [--height 800] [--timeout 45] [--a11y-lines 120]
//!
//! Everything runs in one process (`multiprocess` is a cargo feature, but off at runtime by default),
//! so `/usr/bin/time -l` measures the whole engine.

use std::cell::{Cell, RefCell};
use std::collections::HashMap;
use std::rc::Rc;
use std::time::{Duration, Instant};

use cookie::Cookie;
use dpi::PhysicalSize;
use euclid::Point2D;
use servo::accesskit::{self, Action, ActionRequest, NodeId, Role, TreeId, TreeUpdate};
use servo::{
    ConsoleLogLevel, CookieSource, CSSPixel, EventLoopWaker, InputEvent, JSValue, Key, KeyState, KeyboardEvent,
    LoadStatus, MouseButton, MouseButtonAction, MouseButtonEvent, MouseMoveEvent, Preferences,
    RenderingContext, Servo, ServoBuilder, SoftwareRenderingContext, WebView, WebViewBuilder,
    WebViewDelegate, WebViewPoint,
};
use url::Url;

struct Args {
    url: Url,
    out: String,
    form_test: bool,
    two_instances: bool,
    experimental: bool,
    width: u32,
    height: u32,
    timeout: Duration,
    a11y_lines: usize,
}

fn parse_args() -> Args {
    let mut it = std::env::args().skip(1);
    let mut url = None;
    let mut out = "shot.png".to_string();
    let (mut form_test, mut two_instances, mut experimental) = (false, false, false);
    let (mut width, mut height, mut timeout, mut a11y_lines) = (1280, 800, 45u64, 120usize);
    while let Some(a) = it.next() {
        match a.as_str() {
            "--out" => out = it.next().expect("--out value"),
            "--form-test" => form_test = true,
            "--two-instances" => two_instances = true,
            "--experimental" => experimental = true,
            "--width" => width = it.next().unwrap().parse().unwrap(),
            "--height" => height = it.next().unwrap().parse().unwrap(),
            "--timeout" => timeout = it.next().unwrap().parse().unwrap(),
            "--a11y-lines" => a11y_lines = it.next().unwrap().parse().unwrap(),
            other => url = Some(Url::parse(other).expect("valid URL")),
        }
    }
    Args {
        url: url.expect("usage: servo-spike <url> [flags]"),
        out,
        form_test,
        two_instances,
        experimental,
        width,
        height,
        timeout: Duration::from_secs(timeout),
        a11y_lines,
    }
}

#[derive(Clone)]
struct NoopWaker;
impl EventLoopWaker for NoopWaker {
    fn clone_box(&self) -> Box<dyn EventLoopWaker> {
        Box::new(self.clone())
    }
    fn wake(&self) {}
}

struct Delegate {
    start: Instant,
    first_frame: Cell<Option<Duration>>,
    load_complete: Cell<Option<Duration>>,
    frames: Cell<u32>,
    a11y_updates: RefCell<Vec<TreeUpdate>>,
}

impl WebViewDelegate for Delegate {
    fn notify_new_frame_ready(&self, webview: WebView) {
        if self.first_frame.get().is_none() {
            self.first_frame.set(Some(self.start.elapsed()));
        }
        self.frames.set(self.frames.get() + 1);
        webview.paint();
    }
    fn notify_load_status_changed(&self, _webview: WebView, status: LoadStatus) {
        if status == LoadStatus::Complete && self.load_complete.get().is_none() {
            self.load_complete.set(Some(self.start.elapsed()));
        }
    }
    fn show_console_message(&self, _webview: WebView, level: ConsoleLogLevel, message: String) {
        if matches!(level, ConsoleLogLevel::Error | ConsoleLogLevel::Warn) {
            let m: String = message.replace('\n', " ").chars().take(200).collect();
            println!("  console {level:?}: {m}");
        }
    }
    fn notify_accessibility_tree_update(&self, _webview: WebView, update: TreeUpdate) {
        self.a11y_updates.borrow_mut().push(update);
    }
}

/// Spin the event loop until `done()` or the deadline. Returns false on timeout.
fn spin(servo: &Servo, deadline: Instant, done: impl Fn() -> bool) -> bool {
    while !done() {
        if Instant::now() > deadline {
            return false;
        }
        servo.spin_event_loop();
        std::thread::sleep(Duration::from_millis(1));
    }
    true
}

fn spin_for(servo: &Servo, d: Duration) {
    let end = Instant::now() + d;
    spin(servo, end, || false);
}

fn eval(servo: &Servo, webview: &WebView, script: &str) -> String {
    let slot: Rc<RefCell<Option<String>>> = Rc::new(RefCell::new(None));
    let s2 = slot.clone();
    webview.evaluate_javascript(script, move |r| {
        *s2.borrow_mut() = Some(match r {
            Ok(JSValue::String(s)) => s,
            Ok(v) => format!("{v:?}"),
            Err(e) => format!("JS error: {e:?}"),
        })
    });
    let ok = spin(servo, Instant::now() + Duration::from_secs(10), || slot.borrow().is_some());
    if !ok {
        return "<eval timed out>".into();
    }
    slot.borrow_mut().take().unwrap()
}

// ---------- accessibility ----------

#[derive(Default)]
struct A11yTree {
    nodes: HashMap<(TreeId, NodeId), accesskit::Node>,
    roots: HashMap<TreeId, NodeId>,
    first_tree: Option<TreeId>,
}

impl A11yTree {
    fn apply(&mut self, u: &TreeUpdate) {
        if let Some(t) = &u.tree {
            self.roots.insert(u.tree_id, t.root);
            self.first_tree.get_or_insert(u.tree_id);
        }
        for (id, n) in &u.nodes {
            self.nodes.insert((u.tree_id, *id), n.clone());
        }
    }

    fn dump(&self, max_lines: usize) -> Vec<String> {
        let mut out = Vec::new();
        if let Some(t) = self.first_tree {
            self.walk(t, self.roots[&t], 0, &mut out, max_lines);
        }
        out
    }

    fn walk(&self, t: TreeId, id: NodeId, depth: usize, out: &mut Vec<String>, max: usize) {
        if out.len() >= max {
            return;
        }
        let Some(n) = self.nodes.get(&(t, id)) else { return };
        let name = n.label().or(n.value()).map(|s| s.replace('\n', " ")).unwrap_or_default();
        let boring = (matches!(n.role(), Role::GenericContainer | Role::Paragraph) && name.is_empty())
            || (n.role() == Role::TextRun && name.trim().trim_matches('\u{200b}').is_empty());
        let next_depth = if boring { depth } else { depth + 1 };
        if !boring {
            let name: String = name.chars().take(80).collect();
            out.push(format!("{}{:?} {:?}", "  ".repeat(depth), n.role(), name));
        }
        // Grafted subtree (e.g. iframe).
        if let Some(sub) = n.tree_id() {
            if let Some(r) = self.roots.get(&sub) {
                self.walk(sub, *r, next_depth, out, max);
            }
        }
        for c in n.children() {
            self.walk(t, *c, next_depth, out, max);
        }
    }

    fn find(&self, pred: impl Fn(&accesskit::Node) -> bool) -> Option<(TreeId, NodeId)> {
        self.nodes.iter().find(|(_, n)| pred(n)).map(|(k, _)| *k)
    }
}

fn type_text(webview: &WebView, text: &str) {
    for ch in text.chars() {
        for state in [KeyState::Down, KeyState::Up] {
            webview.notify_input_event(InputEvent::Keyboard(KeyboardEvent::from_state_and_key(
                state,
                Key::Character(ch.to_string()),
            )));
        }
    }
}

fn click_page(webview: &WebView, x: f32, y: f32) {
    let p = WebViewPoint::Page(Point2D::<f32, CSSPixel>::new(x, y));
    webview.notify_input_event(InputEvent::MouseMove(MouseMoveEvent::new(p)));
    for action in [MouseButtonAction::Down, MouseButtonAction::Up] {
        webview.notify_input_event(InputEvent::MouseButton(MouseButtonEvent::new(
            action,
            MouseButton::Primary,
            p,
        )));
    }
}

fn center_of(servo: &Servo, webview: &WebView, sel: &str) -> Option<(f32, f32)> {
    let js = format!(
        "(() => {{ const r = document.querySelector('{sel}').getBoundingClientRect(); return (r.x + r.width/2) + ',' + (r.y + r.height/2); }})()"
    );
    let s = eval(servo, webview, &js);
    let mut it = s.split(',').map(|v| v.trim().parse::<f32>());
    Some((it.next()?.ok()?, it.next()?.ok()?))
}

fn main() {
    let args = parse_args();
    let start = Instant::now();
    let log = |msg: String| println!("[{:>7.3}s] {msg}", start.elapsed().as_secs_f64());

    let rendering_context = Rc::new(
        SoftwareRenderingContext::new(PhysicalSize::new(args.width, args.height))
            .expect("SoftwareRenderingContext"),
    );
    rendering_context.make_current().expect("make_current");

    let mut prefs = Preferences::default();
    prefs.accessibility_enabled = true;
    if args.experimental {
        // Prefs that are off by default in 0.7.0 but common on real sites.
        prefs.dom_intersection_observer_enabled = true;
        prefs.dom_resize_observer_enabled = true;
        prefs.dom_crypto_subtle_enabled = true;
        prefs.dom_fontface_enabled = true;
        prefs.dom_indexeddb_enabled = true;
        prefs.dom_web_animations_enabled = true;
        prefs.dom_adoptedstylesheet_enabled = true;
    }
    let servo = ServoBuilder::default()
        .preferences(prefs)
        .event_loop_waker(Box::new(NoopWaker))
        .build();
    log("servo built".into());

    if args.two_instances {
        let r = std::panic::catch_unwind(|| {
            ServoBuilder::default().event_loop_waker(Box::new(NoopWaker)).build();
        });
        match r {
            Ok(()) => log("second Servo instance: built OK".into()),
            Err(e) => {
                let msg = e
                    .downcast_ref::<String>()
                    .cloned()
                    .or_else(|| e.downcast_ref::<&str>().map(|s| s.to_string()))
                    .unwrap_or_default();
                log(format!("second Servo instance: PANIC: {msg}"));
            },
        }
    }

    // --- cookie import before load (storageState-like) ---
    let sdm = servo.site_data_manager();
    sdm.set_cookie_for_url(
        args.url.clone(),
        Cookie::build(("spike_plain", "hello")).path("/").build(),
        None,
    );
    sdm.set_cookie_for_url(
        args.url.clone(),
        Cookie::build(("spike_httponly", "secret")).path("/").http_only(true).build(),
        None,
    );
    log("imported 2 cookies (spike_plain, spike_httponly[HttpOnly])".into());

    let delegate = Rc::new(Delegate {
        start,
        first_frame: Cell::new(None),
        load_complete: Cell::new(None),
        frames: Cell::new(0),
        a11y_updates: RefCell::new(Vec::new()),
    });
    let webview = WebViewBuilder::new(&servo, rendering_context.clone())
        .delegate(delegate.clone())
        .url(args.url.clone())
        .build();
    let a11y_tree_id = webview.set_accessibility_active(true);
    log(format!("webview created, a11y tree id: {a11y_tree_id:?}"));

    let deadline = Instant::now() + args.timeout;
    let loaded = spin(&servo, deadline, || webview.load_status() == LoadStatus::Complete);
    log(format!(
        "load complete={loaded} (delegate load_complete={:?}, first_frame={:?})",
        delegate.load_complete.get(),
        delegate.first_frame.get()
    ));
    if !loaded {
        log(format!("load status at timeout: {:?}", webview.load_status()));
    }
    log(format!("url={:?} title={:?}", webview.url().map(|u| u.to_string()), webview.page_title()));

    // Let late frames / a11y updates arrive.
    spin_for(&servo, Duration::from_millis(1500));
    log(format!("first frame at {:?}, frames so far {}", delegate.first_frame.get(), delegate.frames.get()));
    log(format!(
        "performance paint entries: {}",
        eval(&servo, &webview,
            "JSON.stringify(performance.getEntriesByType('paint').map(e => [e.name, Math.round(e.startTime)]))")
    ));

    // --- screenshot ---
    let shot: Rc<RefCell<Option<Result<servo::RgbaImage, String>>>> = Rc::new(RefCell::new(None));
    let s2 = shot.clone();
    let t0 = Instant::now();
    webview.take_screenshot(None, move |r| *s2.borrow_mut() = Some(r.map_err(|e| format!("{e:?}"))));
    spin(&servo, Instant::now() + Duration::from_secs(20), || shot.borrow().is_some());
    match shot.borrow_mut().take() {
        Some(Ok(img)) => {
            img.save(&args.out).expect("save png");
            log(format!("screenshot {}x{} -> {} in {:?}", img.width(), img.height(), args.out, t0.elapsed()));
        },
        Some(Err(e)) => log(format!("screenshot error: {e}")),
        None => log("screenshot timed out".into()),
    }

    // --- accessibility tree ---
    let mut tree = A11yTree::default();
    for u in delegate.a11y_updates.borrow().iter() {
        tree.apply(u);
    }
    log(format!(
        "a11y: {} updates, {} nodes, {} trees",
        delegate.a11y_updates.borrow().len(),
        tree.nodes.len(),
        tree.roots.len()
    ));
    for line in tree.dump(args.a11y_lines) {
        println!("    {line}");
    }

    // --- cookies export ---
    for (label, src) in [("HTTP", CookieSource::HTTP), ("NonHTTP", CookieSource::NonHTTP)] {
        let cookies = sdm.cookies_for_url(args.url.clone(), src);
        log(format!("cookies_for_url({label}): {} cookies", cookies.len()));
        for c in cookies {
            let v: String = c.value().chars().take(24).collect();
            println!(
                "    {}={} httponly={:?} secure={:?} domain={:?} path={:?} expires={:?} samesite={:?}",
                c.name(), v, c.http_only(), c.secure(), c.domain(), c.path(), c.expires(), c.same_site()
            );
        }
    }
    log(format!("document.cookie = {}", eval(&servo, &webview, "document.cookie")));
    log(format!("navigator.userAgent = {}", eval(&servo, &webview, "navigator.userAgent")));

    // --- input injection ---
    if args.form_test {
        if let Some((x, y)) = center_of(&servo, &webview, "#user") {
            click_page(&webview, x, y);
            spin_for(&servo, Duration::from_millis(200));
            log(format!("clicked #user at ({x},{y}); activeElement={}", eval(&servo, &webview, "document.activeElement && document.activeElement.id")));
            type_text(&webview, "alice");
            spin_for(&servo, Duration::from_millis(300));
            log(format!("#user.value after typing = {:?}", eval(&servo, &webview, "document.getElementById('user').value")));
        }
        // Re-read the a11y tree after the edit, then click the button through AccessKit.
        spin_for(&servo, Duration::from_millis(500));
        let mut tree = A11yTree::default();
        for u in delegate.a11y_updates.borrow().iter() {
            tree.apply(u);
        }
        let textbox = tree.find(|n| n.role() == Role::TextInput);
        if let Some(k) = textbox {
            log(format!("a11y textbox after typing: value={:?}", tree.nodes[&k].value()));
        } else {
            log(format!("a11y: no TextInput node; {} updates total after typing", delegate.a11y_updates.borrow().len()));
        }
        // Servo 0.7.0 emits no Button role; fall back to the TextRun inside the button.
        match tree.find(|n| n.role() == Role::Button).or_else(|| tree.find(|n| n.role() == Role::TextRun && n.value() == Some("Log in"))) {
            Some((target_tree, target_node)) => {
                servo.forward_accessibility_action(ActionRequest {
                    action: Action::Click,
                    target_tree,
                    target_node,
                    data: None,
                });
                spin_for(&servo, Duration::from_millis(500));
                log(format!("after AccessKit Click on button: #status = {:?}", eval(&servo, &webview, "document.getElementById('status').textContent")));
            },
            None => log("no Button node in a11y tree".into()),
        }
        let shot2: Rc<RefCell<Option<servo::RgbaImage>>> = Rc::new(RefCell::new(None));
        let s3 = shot2.clone();
        webview.take_screenshot(None, move |r| *s3.borrow_mut() = r.ok());
        spin(&servo, Instant::now() + Duration::from_secs(10), || shot2.borrow().is_some());
        if let Some(img) = shot2.borrow_mut().take() {
            let p = args.out.replace(".png", "-after-input.png");
            img.save(&p).unwrap();
            log(format!("post-input screenshot -> {p}"));
        }
    }

    log(format!("done; total frames {}", delegate.frames.get()));
    // Exit without orderly shutdown to keep the spike short.
    std::process::exit(0);
}
