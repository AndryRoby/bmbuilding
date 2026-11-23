/* BM Building ako ukážka Ateliér Olovnica (products/arling-sk/web/ukazky/architekti/app.js, prenesené a prispôsobené):
   pretočenie číslic kót, mobilné menu (Esc, klik mimo, kotva), pripnutý pás realizácií na počítači a šípky voľného
   pásu pre myš, kreslenie schémy až v obraze (mobil, tablet), spodná lišta Zavolať a Napísať e-mail, filter
   a zoradenie realizácií, lightbox s viacerými galériami na stránke (zmluva markup: build.py Web.lb_data
   a Web.lightbox_dialog, opis ops/weby-na-mieru/klienti/bmbuilding/dizajn-olovnica/PRIPRAVA-BUILD.md bod 3).
   Olovnica v mene, kreslenie schémy a odkrytie fotky sú čisté CSS. Pri posune stránky sa tu nepočíta rozloženie:
   pás posúva časová os posunu v CSS, lištu a schému prepínajú pozorovatelia (IntersectionObserver).
   Web ukáže všetok obsah aj bez tohto súboru. Žiadne údaje z obsahu sa nevkladajú ako HTML: len atribúty,
   textContent a presun existujúcich prvkov. */
(function () {
  'use strict';
  window.BMB = 1;
  var d = document;
  var maIO = 'IntersectionObserver' in window;
  var pohyb = !(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  function vsetky(sel, koren) { return Array.prototype.slice.call((koren || d).querySelectorAll(sel)); }
  // Práca, ktorú netreba hneď (pás prác), počká na voľnú chvíľu prehliadača, najneskôr 1,2 s (ukážka, Lighthouse TBT).
  function vKlude(fn) {
    if ('requestIdleCallback' in window) window.requestIdleCallback(fn, { timeout: 1200 });
    else window.setTimeout(fn, 1200);
  }

  // 0. Číslice kót (pás, fázy postupu, detail) sa pri vstupe do obrazu raz pretočia o celé kolo (číslice dodá CSS,
  //    tu sa len pridá trieda). Pri obmedzenom pohybe sa nič nedeje, čísla ostanú, ako sú.
  if (pohyb && maIO) {
    var ioOtoc = new IntersectionObserver(function (zaznamy) {
      zaznamy.forEach(function (z) {
        if (!z.isIntersecting) return;
        z.target.classList.add('otocene');
        ioOtoc.unobserve(z.target);
      });
    }, { threshold: 0.6, rootMargin: '0px 0px -6% 0px' });
    vsetky('.otoc').forEach(function (el) { ioOtoc.observe(el); });
  }

  // 1. Mobilné menu (details funguje aj bez skriptu): pri otvorení text Zavrieť, Esc zavrie a vráti ohnisko na
  //    tlačidlo, klik na kotvu na tej istej stránke alebo klik mimo panelu zavrie.
  vsetky('details.menu-m').forEach(function (menu) {
    var tl = menu.querySelector('summary');
    if (!tl) return;
    var povodny = tl.textContent;
    function zavri(spat) {
      if (!menu.open) return;
      menu.open = false;
      if (spat) tl.focus();
    }
    menu.addEventListener('toggle', function () { tl.textContent = menu.open ? 'Zavrieť' : povodny; });
    menu.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' || e.key === 'Esc') { e.preventDefault(); zavri(true); }
    });
    vsetky('a[href]', menu).forEach(function (a) {
      a.addEventListener('click', function () {
        var h = a.getAttribute('href') || '';
        if (h.charAt(0) === '#') zavri(false);
      });
    });
    d.addEventListener('click', function (e) {
      if (menu.open && !menu.contains(e.target)) zavri(false);
    });
  });

  // 2. Spodná lišta na mobile: vyjde, keď tlačidlá úvodu odídu z obrazu; skryje sa pri sekcii Kontakt a v päte.
  var lista = d.querySelector('.lista');
  if (lista && maIO) {
    var vidno = new Map();
    var ioLista = new IntersectionObserver(function (zaznamy) {
      zaznamy.forEach(function (z) { vidno.set(z.target, z.isIntersecting); });
      var niecoVidno = false;
      vidno.forEach(function (v) { if (v) niecoVidno = true; });
      lista.classList.toggle('vidno', !niecoVidno);
    });
    vsetky('[data-vyzva-hore], [data-vyzva-dole]').forEach(function (el) { ioLista.observe(el); });
  } else if (lista) {
    lista.classList.add('vidno');
  }

  // 3. Schéma v úvode sa kreslí, keď ju človek vidí. Na počítači je v prvej obrazovke a kreslí sa hneď (čisté CSS).
  //    Na mobile a tablete je pod fotkou: ak je pri načítaní z väčšej časti mimo obrazu, jej pohyb sa vráti
  //    na začiatok a podrží (trieda caka), kým nepríde do obrazu. Polohu dodá prvé hlásenie pozorovateľa.
  (function () {
    if (!pohyb || !maIO) return;
    var plan = d.querySelector('.uvod-plan');
    if (!plan) return;
    var prve = true;
    var io = new IntersectionObserver(function (zaznamy) {
      var z = zaznamy[zaznamy.length - 1];
      if (!z) return;
      if (prve) {
        prve = false;
        var vyska = (z.rootBounds && z.rootBounds.height) || window.innerHeight;
        if (z.boundingClientRect.top < vyska * 0.72) { io.disconnect(); return; }
        plan.classList.add('caka');
        if (plan.getAnimations) {
          plan.getAnimations({ subtree: true }).forEach(function (a) { try { a.currentTime = 0; } catch (e) { /* nič */ } });
        }
      }
      if (z.isIntersecting) { plan.classList.remove('caka'); io.disconnect(); }
    }, { threshold: 0.3 });
    io.observe(plan);
  })();

  // Odložená fotka karty pásu (ukážka): skutočný zdroj je v data-src a data-srcset, kým sa k nej človek nepriblíži.
  //    Bez skriptu ju nahradí kópia v noscript. AVIF (data-avif): fotka sa obalí do picture so zdrojom
  //    type=image/avif, prehliadač bez AVIF ho preskočí a vezme WebP zo srcset (linka bmb-fotky).
  function avifZdroj(img, srcset, sizes) {
    var obal = img.parentNode;
    if (!obal) return null;
    if (obal.nodeName !== 'PICTURE') {
      var p = d.createElement('picture');
      obal.insertBefore(p, img);
      p.appendChild(img);
      obal = p;
    }
    var z = obal.querySelector('source[type="image/avif"]');
    if (!z) {
      z = d.createElement('source');
      z.setAttribute('type', 'image/avif');
      obal.insertBefore(z, img);
    }
    if (sizes) z.setAttribute('sizes', sizes);
    if (srcset) z.setAttribute('srcset', srcset); else z.removeAttribute('srcset');
    return z;
  }
  function nacitaj(img) {
    var src = img.getAttribute('data-src');
    if (!src) return;
    var avif = img.getAttribute('data-avif');
    if (avif) {
      avifZdroj(img, avif, img.getAttribute('sizes'));
      img.removeAttribute('data-avif');
    }
    var srcset = img.getAttribute('data-srcset');
    if (pohyb) {
      img.classList.add('nacitava');
      var hotovo = function () { img.classList.remove('nacitava'); };
      img.addEventListener('load', hotovo, { once: true });
      img.addEventListener('error', hotovo, { once: true });
    }
    if (srcset) img.setAttribute('srcset', srcset);
    img.setAttribute('src', src);
    img.removeAttribute('data-srcset');
    img.removeAttribute('data-src');
  }

  // 4. Pás vybraných realizácií (ukážka 7). Fotky sa načítajú až pri priblížení: najprv keď je sekcia do štvrtiny
  //    výšky okna pod obrazom, na mobile potom len karty pri viditeľnej časti pásu. Na počítači sa pás pripne
  //    a zvislý posun ho posúva do strany cez animation-timeline v CSS; kde ho prehliadač nepozná, ostáva voľný
  //    posuvník so šípkami pre myš. Celé sa spúšťa až vo voľnej chvíli (vKlude).
  vKlude(function () {
    var sekcia = d.querySelector('.prace');
    var lepidlo = sekcia && sekcia.querySelector('.prace-lepidlo');
    var obal = sekcia && sekcia.querySelector('.prace-posuvne');
    var pas = sekcia && sekcia.querySelector('.pas');
    if (!sekcia || !lepidlo || !obal || !pas) return;

    var odlozene = vsetky('img[data-src]', sekcia);
    function nacitajVsetky() { odlozene.forEach(nacitaj); }
    var fotkyKariet = vsetky('.projekt-foto img', sekcia);
    var blizko = false;
    var ioPas = null;
    function priblizenie() {
      blizko = true;
      if (jePripnute()) { cakajNaPosun(); return; }
      if (!maIO) { nacitajVsetky(); return; }
      ioPas = new IntersectionObserver(function (zaznamy) {
        zaznamy.forEach(function (z) {
          if (z.isIntersecting) { nacitaj(z.target); ioPas.unobserve(z.target); }
        });
      }, { root: pas, rootMargin: '0px 60% 0px 60%' });
      odlozene.forEach(function (img) { if (img.hasAttribute('data-src')) ioPas.observe(img); });
    }
    // Šírka fotiek pre výber zo srcset: v HTML je šírka voľného pásu, pripnutý pás má karty min(56vh, 600px) * 0,8.
    fotkyKariet.forEach(function (img) { img.setAttribute('data-sizes-volne', img.getAttribute('sizes') || ''); });
    function nastavSizes(pripnute) {
      fotkyKariet.forEach(function (img) {
        var s = pripnute ? 'min(45vh, 480px)' : img.getAttribute('data-sizes-volne');
        if (s && img.getAttribute('sizes') !== s) img.setAttribute('sizes', s);
        // zdroj AVIF v picture (po načítaní) má vlastné sizes, prehliadač vyberá podľa neho
        var z = img.parentNode && img.parentNode.nodeName === 'PICTURE' ? img.parentNode.querySelector('source') : null;
        if (s && z && z.getAttribute('sizes') !== s) z.setAttribute('sizes', s);
      });
    }

    var podporaCasovejOsi = !!(window.CSS && CSS.supports && CSS.supports('animation-timeline: view()') &&
      CSS.supports('animation-range: contain 0% contain 100%'));
    var podmienka = window.matchMedia ? window.matchMedia('(min-width: 1024px) and (min-height: 560px) and (prefers-reduced-motion: no-preference)') : null;
    var posun = 0;

    function jePripnute() { return sekcia.classList.contains('pripnute'); }
    function prepocitaj() {
      var zapnut = podporaCasovejOsi && podmienka && podmienka.matches;
      if (!zapnut) {
        if (jePripnute()) {
          sekcia.classList.remove('pripnute');
          sekcia.style.height = '';
          sekcia.style.removeProperty('--posun');
          pas.setAttribute('tabindex', '0');
          nastavSizes(false);
        }
        stavSipok();
        return;
      }
      sekcia.classList.add('pripnute');
      nastavSizes(true);
      pas.removeAttribute('tabindex');
      posun = Math.max(0, Math.round(obal.scrollWidth - lepidlo.clientWidth));
      // CSSOM (nie atribút style): CSP webu atribút style nedovolí
      sekcia.style.setProperty('--posun', posun + 'px');
      sekcia.style.height = (window.innerHeight + posun) + 'px';
      if (blizko) cakajNaPosun();
    }
    // Pripnutý pás (počítač) začína hneď pod úvodom: jeho fotky sa načítajú pri prvom posune stránky, pri fokuse
    // v páse alebo keď sekcia naozaj vojde do obrazu (napríklad po skoku na #prace).
    var caka = false;
    function cakajNaPosun() {
      if (caka) return;
      caka = true;
      var hotovo = false;
      var ioTu = null;
      function teraz() {
        if (hotovo) return;
        hotovo = true;
        window.removeEventListener('scroll', teraz);
        sekcia.removeEventListener('focusin', teraz);
        if (ioTu) ioTu.disconnect();
        nacitajVsetky();
      }
      window.addEventListener('scroll', teraz, { passive: true });
      sekcia.addEventListener('focusin', teraz);
      if (maIO) {
        ioTu = new IntersectionObserver(function (z) { if (z[0] && z[0].isIntersecting) teraz(); }, { rootMargin: '0px 0px -12% 0px' });
        ioTu.observe(sekcia);
      }
    }

    // Voľný pás (mobil, tablet, Firefox bez časovej osi, obmedzený pohyb): tlačidlá Predošlá a Ďalšia pre myš
    // posunú pás o šírku karty; na začiatku a na konci sú vypnuté (aria-disabled, fokus na nich ostane).
    var sipky = sekcia.querySelector('.sipky');
    var tlacidla = sipky ? vsetky('.sipka', sipky) : [];
    function krok() {
      var karta = pas.querySelector('li');
      var medzera = parseFloat(window.getComputedStyle(pas).columnGap) || 0;
      return karta ? karta.getBoundingClientRect().width + medzera : pas.clientWidth * 0.8;
    }
    function stavSipok() {
      if (!tlacidla.length) return;
      var koniec = pas.scrollWidth - pas.clientWidth - 2;
      var vlavo = pas.scrollLeft;
      tlacidla.forEach(function (b) {
        var vypnute = Number(b.getAttribute('data-smer')) < 0 ? vlavo <= 2 : vlavo >= koniec;
        var hodnota = vypnute ? 'true' : 'false';
        if (b.getAttribute('aria-disabled') !== hodnota) b.setAttribute('aria-disabled', hodnota);
      });
    }
    tlacidla.forEach(function (b) {
      b.addEventListener('click', function () {
        if (b.getAttribute('aria-disabled') === 'true' || jePripnute()) return;
        nacitajVsetky();
        pas.scrollBy({ left: Number(b.getAttribute('data-smer')) * krok(), behavior: pohyb ? 'smooth' : 'auto' });
      });
    });
    // Pri prvom posune pásu sa načítajú všetky zvyšné karty, aby rýchly šuch neukázal prázdne karty.
    var cakaSnimka = false;
    pas.addEventListener('scroll', function () {
      if (odlozene.some(function (img) { return img.hasAttribute('data-src'); })) nacitajVsetky();
      if (cakaSnimka) return;
      cakaSnimka = true;
      window.requestAnimationFrame(function () { cakaSnimka = false; stavSipok(); });
    }, { passive: true });

    // Fokus klávesnicou v pripnutom páse: posunie stránku tak, aby bola karta celá v obraze. Len fokus z klávesnice
    // (:focus-visible) a len keď karta nie je celá v obraze: stlačenie myši na tlačidle Fotky dá fokus tiež
    // a posun by kartu odsunul spod kurzora, pustenie by padlo inde a lightbox by sa neotvoril (overenie kolo 1).
    pas.addEventListener('focusin', function (e) {
      if (!jePripnute() || posun <= 0) return;
      var t = e.target;
      try { if (t.matches && !t.matches(':focus-visible')) return; } catch (x) { /* prehliadač bez :focus-visible */ }
      var karta = t.closest('.projekt, .projekt-dalsi');
      if (!karta) return;
      var rk = karta.getBoundingClientRect();
      if (rk.left >= 0 && rk.right <= window.innerWidth) return;
      var okraj = parseFloat(window.getComputedStyle(obal).paddingLeft) || 0;
      var ciel = Math.min(posun, Math.max(0, karta.offsetLeft - okraj));
      var dlzka = sekcia.offsetHeight - window.innerHeight;
      var hore = sekcia.getBoundingClientRect().top + window.pageYOffset;
      window.scrollTo(0, Math.round(hore + (ciel / posun) * dlzka));
    });

    window.addEventListener('resize', prepocitaj);
    if (podmienka) {
      if (podmienka.addEventListener) podmienka.addEventListener('change', prepocitaj);
      else if (podmienka.addListener) podmienka.addListener(prepocitaj);
    }
    prepocitaj();
    if (sipky) sipky.hidden = false;
    if (d.readyState !== 'complete') window.addEventListener('load', prepocitaj);
    if (d.fonts && d.fonts.status !== 'loaded' && d.fonts.ready) d.fonts.ready.then(prepocitaj);
    if (maIO) {
      var ioSekcia = new IntersectionObserver(function (zaznamy) {
        if (zaznamy[0] && zaznamy[0].isIntersecting) { ioSekcia.disconnect(); priblizenie(); }
      }, { rootMargin: '0px 0px 25% 0px' });
      ioSekcia.observe(sekcia);
    } else {
      nacitajVsetky();
    }
  });

  // 4b. Kotva z inej stránky (menu Činnosť alebo Postup z podstránky, napríklad ../#sluzby): pripnutý pás zmení výšku
  //     úvodu až po prvom posune prehliadača na kotvu, takže človek skončil v páse a musel kliknúť znova (Andrej
  //     23. 11. 2026). Po zostavení stránky, po načítaní a po písmach sa znova postaví na cieľ kotvy, kým sa sám nepohol.
  (function () {
    var hash = window.location.hash;
    if (!hash || hash.length < 2) return;
    var ciel = null;
    try { ciel = d.getElementById(decodeURIComponent(hash.slice(1))); } catch (e) { ciel = null; }
    if (!ciel) return;
    var pohol = false;
    ['wheel', 'touchstart', 'keydown', 'mousedown'].forEach(function (t) {
      window.addEventListener(t, function () { pohol = true; }, { passive: true, once: true });
    });
    var skoc = function () {
      if (pohol) return;
      try { ciel.scrollIntoView({ block: 'start', behavior: 'instant' }); } catch (e) { ciel.scrollIntoView(true); }
    };
    // po zmene výšky pásu (prepocitaj pri načítaní a po písmach) a ešte raz o chvíľu, keby prehliadač dobiehal vlastný posun
    var neskor = function () { window.requestAnimationFrame(skoc); window.setTimeout(skoc, 150); window.setTimeout(skoc, 600); };
    neskor();
    if (d.readyState !== 'complete') window.addEventListener('load', neskor);
    if (d.fonts && d.fonts.ready) d.fonts.ready.then(neskor);
  })();
  // Plynulý posun (CSS html.plynuly-posun) až po načítaní stránky, aby príchod s kotvou skočil hneď na miesto.
  (function () {
    var zapni = function () { window.setTimeout(function () { d.documentElement.classList.add('plynuly-posun'); }, 700); };
    if (d.readyState === 'complete') zapni(); else window.addEventListener('load', zapni);
  })();

  // 5. Filter, zoradenie a postupné načítanie realizácií: stav v adrese ?druh=&roky=&mesto=&zoradit=, počet
  //    v aria-live, prázdny výsledok. Všetky karty sú v HTML (bez skriptu viditeľné všetky); filter a zoradenie
  //    idú cez všetky karty, z výsledku sa ukáže prvých data-naraz a tlačidlo Zobraziť ďalšie s počtom zvyšných.
  //    Po kliknutí ide fokus na nadpis prvej novej karty (klávesnica pokračuje tam, kde skončila).
  var filtre = d.querySelector('[data-filtre]');
  var zoznam = d.querySelector('[data-zoznam]');
  if (filtre && zoznam) {
    var dlazdice = vsetky('.dlazdica', zoznam);
    var tlacidlaF = vsetky('[data-druh]', filtre);
    var roky = filtre.querySelector('[data-roky]');
    var mesto = filtre.querySelector('[data-mesto]');
    var zoradit = filtre.querySelector('[data-zoradit]');
    var stav = d.querySelector('[data-stav]');
    var prazdne = d.querySelector('[data-prazdne]');
    var zrusit = d.querySelector('[data-zrusit]');
    var viacObal = d.querySelector('[data-viac-obal]');
    var viac = d.querySelector('[data-viac]');
    var viacPocet = d.querySelector('[data-viac-pocet]');
    var naraz = Math.max(0, Number(zoznam.getAttribute('data-naraz')) || 0);
    var ukazat = naraz;
    var param = new URLSearchParams(window.location.search);
    var s = { druh: param.get('druh') || '', roky: param.get('roky') || '', mesto: param.get('mesto') || '',
      zoradit: param.get('zoradit') || 'najnovsie' };
    var poradia = zoradit ? vsetky('option', zoradit).map(function (o) { return o.value; }) : ['najnovsie'];
    if (!tlacidlaF.some(function (b) { return b.getAttribute('data-druh') === s.druh; })) s.druh = '';
    if (roky && !vsetky('option', roky).some(function (o) { return o.value === s.roky; })) s.roky = '';
    if (!mesto || !vsetky('option', mesto).some(function (o) { return o.value === s.mesto; })) s.mesto = '';
    if (poradia.indexOf(s.zoradit) < 0) s.zoradit = 'najnovsie';
    // názov karty na zoradenie podľa abecedy (slovenské poradie: ch za h, diakritika)
    var abc = (window.Intl && Intl.Collator) ? new Intl.Collator('sk', { sensitivity: 'base', numeric: true }) : null;
    var nazvy = new Map();
    dlazdice.forEach(function (li) {
      var h = li.querySelector('.dlazdica-nazov');
      nazvy.set(li, h ? h.textContent.replace(/\s+/g, ' ').trim() : '');
    });
    function porovnaj(a, b) {
      var x = Number(a.getAttribute('data-poradie')), y = Number(b.getAttribute('data-poradie'));
      if (s.zoradit === 'nazov') {
        var na = nazvy.get(a), nb = nazvy.get(b);
        var c = abc ? abc.compare(na, nb) : (na < nb ? -1 : na > nb ? 1 : 0);
        return c || x - y;
      }
      return s.zoradit === 'najstarsie' ? y - x : x - y;
    }
    // Fotky ďalšej dávky (karty, ktoré ukáže najbližšie Zobraziť ďalšie) sa načítajú vopred, keď sa človek
    // priblíži k tlačidlu, aby sa po kliknutí neobjavovali postupne (Andrej 23. 11. 2026: „pri načítaní to tak
    // divne vyzerá“). Obrázky v skrytej karte s loading="eager" prehliadač stiahne, s lazy nie.
    var dalsiaDavka = [];
    var predNacitaj = function () {
      dalsiaDavka.forEach(function (li) {
        vsetky('img[loading="lazy"]', li).forEach(function (img) { img.setAttribute('loading', 'eager'); });
      });
    };
    var obnov = function (zapis, novaPrva) {
      var n = 0;
      var m = 0;
      var prvaNova = null;
      var poradie = dlazdice.slice().sort(porovnaj);
      dalsiaDavka = [];
      poradie.forEach(function (li) {
        var druhy = ' ' + (li.getAttribute('data-druh') || '') + ' ';
        var rok = Number(li.getAttribute('data-rok')) || 0;
        var dekada = rok ? String(rok - rok % 10) : '0';
        var ok = (!s.druh || druhy.indexOf(' ' + s.druh + ' ') >= 0) && (!s.roky || s.roky === dekada)
          && (!s.mesto || li.getAttribute('data-mesto') === s.mesto);
        if (ok && naraz && n >= ukazat && n < ukazat + naraz) dalsiaDavka.push(li);
        var vidno = ok && (!naraz || n < ukazat);
        if (ok) n++;
        if (vidno) {
          m++;
          if (novaPrva !== undefined && m === novaPrva + 1) prvaNova = li;
        }
        if (li.hidden === vidno) li.hidden = !vidno;
        zoznam.appendChild(li);
      });
      tlacidlaF.forEach(function (b) { b.setAttribute('aria-pressed', b.getAttribute('data-druh') === s.druh ? 'true' : 'false'); });
      if (roky) roky.value = s.roky;
      if (mesto) mesto.value = s.mesto;
      if (zoradit) zoradit.value = s.zoradit;
      var filtruje = s.druh || s.roky || s.mesto;
      var text = '';
      if (filtruje) text = 'Vybrané: ' + n + ' z ' + dlazdice.length + (m < n ? ', zobrazené: ' + m : '');
      else if (m < n) text = 'Zobrazené: ' + m + ' z ' + n;
      if (stav && stav.textContent !== text) stav.textContent = text;
      if (prazdne) prazdne.hidden = n > 0;
      if (viacObal) viacObal.hidden = !(m < n);
      if (viacPocet) viacPocet.textContent = m < n ? '(' + (n - m) + ')' : '';
      if (prvaNova) {
        var odkaz = prvaNova.querySelector('.dlazdica-nazov a');
        // preventScroll: bez neho prehliadač posunul stránku k nadpisu prvej novej karty (pri 1600 px o 798 px,
        // Andrej 23. 11. 2026: „ako keby skočí“). Nové karty sa ukážu tam, kde bolo tlačidlo, fokus ide za nimi.
        if (odkaz) odkaz.focus({ preventScroll: true });
      }
      if (zapis) {
        var p = new URLSearchParams();
        if (s.druh) p.set('druh', s.druh);
        if (s.roky) p.set('roky', s.roky);
        if (s.mesto) p.set('mesto', s.mesto);
        if (s.zoradit !== 'najnovsie') p.set('zoradit', s.zoradit);
        var q = p.toString();
        window.history.replaceState(null, '', window.location.pathname + (q ? '?' + q : ''));
      }
    };
    // nový výber alebo poradie začína znova prvými data-naraz kartami
    var zmena = function () { ukazat = naraz; obnov(true); };
    tlacidlaF.forEach(function (b) {
      b.addEventListener('click', function () { s.druh = b.getAttribute('data-druh'); zmena(); });
    });
    if (roky) roky.addEventListener('change', function () { s.roky = roky.value; zmena(); });
    if (mesto) mesto.addEventListener('change', function () { s.mesto = mesto.value; zmena(); });
    if (zoradit) zoradit.addEventListener('change', function () { s.zoradit = zoradit.value; zmena(); });
    if (zrusit) zrusit.addEventListener('click', function () { s.druh = ''; s.roky = ''; s.mesto = ''; zmena(); });
    if (viac) viac.addEventListener('click', function () {
      var bolo = ukazat;
      predNacitaj();
      ukazat += naraz;
      obnov(false, bolo);
    });
    if (viac) {
      viac.addEventListener('pointerenter', predNacitaj);
      viac.addEventListener('focus', predNacitaj);
    }
    if (viacObal && typeof IntersectionObserver === 'function') {
      new IntersectionObserver(function (z) {
        if (z[0] && z[0].isIntersecting) predNacitaj();
      }, { rootMargin: '0px 0px 900px 0px' }).observe(viacObal);
    }
    filtre.hidden = false;
    obnov(false);
  }

  // 6. Lightbox s viacerými galériami. Markup z build.py: odkazy a[data-lightbox] s data-plna, data-srcset,
  //    data-sirka, data-vyska, data-nazov, data-popis, data-udaje, data-index, data-alt, data-galeria (skupina;
  //    bez neho jedna skupina stránky) a pri vizualizácii data-viz a data-autor; jeden dialog#lightbox s figure,
  //    img.lightbox-foto, štítkom .lightbox-stitok, figcaption (.lightbox-nazov, .lightbox-udaje, .lightbox-pocet)
  //    a tlačidlami data-predosla, data-dalsia, data-zavriet. Spúšťač a.prepinac[data-galeria] (tlačidlo Fotky
  //    v páse) otvorí skupinu na prvej fotke. Bez JS alebo bez dialog.showModal odkaz otvorí plnú fotku alebo galériu.
  //    Modálny dialóg prehliadača sám drží ohnisko vnútri (zvyšok stránky je inert) a zavrie sa klávesom Esc.
  var dlg = d.getElementById('lightbox');
  var vsetkyOdkazy = vsetky('a[data-lightbox]');
  if (dlg && vsetkyOdkazy.length && typeof dlg.showModal === 'function') {
    var foto = dlg.querySelector('.lightbox-foto');
    var figura = dlg.querySelector('.lightbox-figura');
    var stitok = dlg.querySelector('.lightbox-stitok');
    var lbNazov = dlg.querySelector('.lightbox-nazov');
    var lbUdaje = dlg.querySelector('.lightbox-udaje');
    var lbPocet = dlg.querySelector('.lightbox-pocet');
    var tlPredosla = dlg.querySelector('[data-predosla]');
    var tlDalsia = dlg.querySelector('[data-dalsia]');
    var tlZavriet = dlg.querySelector('[data-zavriet]');
    // stránka Oprávnenia: dialóg s data-predpona="" a data-slovo="Osvedčenie" (doklady, nie fotky)
    var predponaLb = dlg.hasAttribute('data-predpona') ? dlg.getAttribute('data-predpona') : 'Fotky: ';
    var slovoLb = dlg.getAttribute('data-slovo') || 'Fotka';
    var skupiny = {};
    var skupinaZ = function (a) {
      var zoz = a.closest('.lb-zoznam');
      return a.getAttribute('data-galeria') || (zoz && zoz.getAttribute('data-galeria')) || 'stranka';
    };
    vsetkyOdkazy.forEach(function (a) {
      var k = skupinaZ(a);
      (skupiny[k] = skupiny[k] || []).push(a);
    });
    var odkazy = [];
    var n = 0;
    var akt = 0;
    var spustac = null;
    var tah = null;
    var poTahu = false;
    if (foto) {
      foto.setAttribute('draggable', 'false');
      foto.setAttribute('sizes', '100vw');
    }
    // zdroj AVIF lightboxu (linka bmb-fotky): fotka v picture so source type=image/avif, srcset podľa odkazu
    // (data-avif, v skrátenej zmluve pásu data-sirky-avif); bez AVIF ostáva len WebP v img
    var lbAvif = foto ? avifZdroj(foto, '', '100vw') : null;
    // Skryté zoznamy pásu majú skrátenú zmluvu (build.py Web.karta_pasu): odkaz bez href s číslom fotky (data-nn)
    // a šírkami (data-sirky); priečinok (data-cesta), názov a údaje sú na zozname ul.lb-zoznam. Cesta k súboru je
    // <data-cesta><nn>-<šírka>.webp ako v galérii; srcset, plná fotka, šírka a alt sa tu doplnia.
    var udajZ = function (a, meno) {
      var v = a.getAttribute(meno);
      if (v !== null) return v;
      var zoz = a.closest('.lb-zoznam');
      return zoz ? (zoz.getAttribute(meno) || '') : '';
    };
    var sirkyZ = function (a, meno) {
      return (a.getAttribute(meno || 'data-sirky') || '').split(' ').filter(function (x) { return x; });
    };
    var suborZ = function (a, w, pripona) {
      return udajZ(a, 'data-cesta') + a.getAttribute('data-nn') + '-' + w + '.' + (pripona || 'webp');
    };
    var plnaZ = function (a) {
      var s = sirkyZ(a);
      return a.getAttribute('data-plna') || a.getAttribute('href') || suborZ(a, s[s.length - 1]);
    };
    // Odkaz galérie s náhľadom (build.py lb_data nahlad) nemá data-srcset ani data-avif: srcset WebP a zdroj AVIF
    // sú tie isté ako pri náhľade v odkaze, berú sa z neho (overenie zostavy bmb-presnost, menšie HTML).
    var nahladZ = function (a, vyber) {
      if (a.hasAttribute('data-sirky')) return '';
      var el = a.querySelector(vyber);
      return (el && el.getAttribute('srcset')) || '';
    };
    var srcsetZ = function (a) {
      var s = a.getAttribute('data-srcset') || nahladZ(a, 'img');
      if (s) return s;
      return sirkyZ(a).map(function (w) { return suborZ(a, w) + ' ' + w + 'w'; }).join(', ');
    };
    var avifZ = function (a) {
      var s = a.getAttribute('data-avif') || (a.hasAttribute('data-srcset') ? '' : nahladZ(a, 'source[type="image/avif"]'));
      if (s) return s;
      return sirkyZ(a, 'data-sirky-avif').map(function (w) { return suborZ(a, w, 'avif') + ' ' + w + 'w'; }).join(', ');
    };
    // prednačítanie susednej fotky: odpojený picture so zdrojom AVIF vyberie ten istý súbor ako lightbox
    var prednacitaj = function (a) {
      var im = new Image();
      var av = avifZ(a);
      if (av) {
        var p = d.createElement('picture');
        var z = d.createElement('source');
        z.setAttribute('type', 'image/avif');
        z.setAttribute('sizes', '100vw');
        z.setAttribute('srcset', av);
        p.appendChild(z);
        p.appendChild(im);
      }
      im.sizes = '100vw';
      im.srcset = srcsetZ(a);
      im.src = plnaZ(a);
    };
    var ukaz = function (i) {
      akt = (i % n + n) % n;
      var a = odkazy[akt];
      var nahlad = a.querySelector('img');
      var viz = a.getAttribute('data-viz') === '1';
      var nazov = udajZ(a, 'data-nazov');
      var sirky = sirkyZ(a);
      if (foto && pohyb && dlg.open) {
        // krátke prelínanie pri výmene fotky (len bez obmedzenia pohybu); trieda sa znova pridá po reflowe
        foto.classList.remove('meni');
        void foto.offsetWidth;
        foto.classList.add('meni');
      }
      if (foto) {
        var sirka = Number(a.getAttribute('data-sirka') || sirky[sirky.length - 1]) || 0;
        foto.setAttribute('loading', 'eager');
        foto.setAttribute('width', sirka ? String(sirka) : '');
        foto.setAttribute('height', a.getAttribute('data-vyska') || '');
        var av = avifZ(a);
        if (lbAvif) { if (av) lbAvif.setAttribute('srcset', av); else lbAvif.removeAttribute('srcset'); }
        foto.setAttribute('srcset', srcsetZ(a));
        foto.setAttribute('src', plnaZ(a));
        // fotka nikdy väčšia než 1,25 násobok šírky súboru (FOTKY.md bod 3), doklad (data-presne) nie väčší než
        // súbor; CSSOM, nie atribút style (CSP)
        var nasobok = a.getAttribute('data-presne') === '1' ? 1 : 1.25;
        foto.style.maxWidth = sirka ? 'min(100%, ' + Math.round(sirka * nasobok) + 'px)' : '';
        foto.alt = a.getAttribute('data-alt') || (nahlad ? nahlad.alt : nazov + (n > 1 ? ', fotka ' + (akt + 1) + ' z ' + n : ''));
      }
      if (stitok) stitok.hidden = !viz;
      if (lbNazov) lbNazov.textContent = nazov;
      if (lbUdaje) {
        // časti údajov delí „ · “ (build.py LB_ODDELOVAC); pri vizualizácii bez projektanta, keď je ním sám autor
        var autor = viz ? a.getAttribute('data-autor') : '';
        var vizText = viz ? 'Vizualizácia' + (autor ? ', autor: ' + autor : '') : '';
        var casti = (udajZ(a, 'data-udaje') || '').split(' · ').filter(function (x) {
          return x && !(autor && x === 'projektant ' + autor);
        });
        lbUdaje.textContent = [vizText, a.getAttribute('data-popis')].concat(casti)
          .filter(function (x) { return x; }).join(' · ');
      }
      if (lbPocet) lbPocet.textContent = n > 1 ? slovoLb + ' ' + (akt + 1) + ' z ' + n : '';
      if (n > 1) {
        prednacitaj(odkazy[(akt + 1) % n]);
        prednacitaj(odkazy[(akt - 1 + n) % n]);
      }
    };
    var otvor = function (skupina, i, kto) {
      odkazy = skupina;
      n = odkazy.length;
      if (!n) return;
      spustac = kto || null;
      if (tlPredosla) tlPredosla.hidden = n < 2;
      if (tlDalsia) tlDalsia.hidden = n < 2;
      dlg.setAttribute('aria-label', predponaLb + udajZ(odkazy[0], 'data-nazov'));
      ukaz(i);
      d.documentElement.classList.add('lb-otvorene');
      dlg.showModal();
      if (tlZavriet) tlZavriet.focus();
    };
    function nechajPrehliadacu(e) {
      // nový panel, okno alebo stiahnutie (Ctrl, Cmd, Shift, Alt, stredné tlačidlo) nechá prehliadaču
      return e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey;
    }
    vsetkyOdkazy.forEach(function (a) {
      a.addEventListener('click', function (e) {
        if (nechajPrehliadacu(e)) return;
        e.preventDefault();
        var skupina = skupiny[skupinaZ(a)];
        otvor(skupina, skupina.indexOf(a), null);
      });
    });
    vsetky('a.prepinac[data-galeria]').forEach(function (b) {
      var skupina = skupiny[b.getAttribute('data-galeria')];
      if (!skupina) return;
      b.addEventListener('click', function (e) {
        if (nechajPrehliadacu(e)) return;
        e.preventDefault();
        otvor(skupina, 0, b);
      });
    });
    // zatvorenie (Esc, tlačidlo, klik vedľa fotky): ohnisko späť na spúšťač (tlačidlo Fotky) alebo na odkaz práve
    // zobrazenej fotky vo viditeľnej galérii
    dlg.addEventListener('close', function () {
      d.documentElement.classList.remove('lb-otvorene');
      tah = null;
      if (spustac) spustac.focus();
      else if (odkazy[akt]) odkazy[akt].focus();
    });
    if (tlZavriet) tlZavriet.addEventListener('click', function () { dlg.close(); });
    if (tlPredosla) tlPredosla.addEventListener('click', function () { ukaz(akt - 1); });
    if (tlDalsia) tlDalsia.addEventListener('click', function () { ukaz(akt + 1); });
    dlg.addEventListener('keydown', function (e) {
      // Tab krúži medzi tlačidlami dialógu (Chrome by inak pustil ohnisko z dokumentu do lišty prehliadača)
      if (e.key === 'Tab') {
        var tl = [tlPredosla, tlDalsia, tlZavriet].filter(function (b) { return b && !b.hidden; });
        if (!tl.length) return;
        var prvy = tl[0];
        var posledny = tl[tl.length - 1];
        var ae = d.activeElement;
        if (e.shiftKey && (ae === prvy || !dlg.contains(ae))) { e.preventDefault(); posledny.focus(); }
        else if (!e.shiftKey && (ae === posledny || !dlg.contains(ae))) { e.preventDefault(); prvy.focus(); }
        return;
      }
      if (n < 2 || e.altKey || e.ctrlKey || e.metaKey) return;
      var k = e.key;
      if (k === 'ArrowRight' || k === 'Right' || k === 'PageDown') ukaz(akt + 1);
      else if (k === 'ArrowLeft' || k === 'Left' || k === 'PageUp') ukaz(akt - 1);
      else if (k === 'Home') ukaz(0);
      else if (k === 'End') ukaz(n - 1);
      else return;
      e.preventDefault();
    });
    // klik na prázdnu plochu vedľa fotky zavrie (nie po potiahnutí, nie na fotke, popise ani tlačidlách)
    dlg.addEventListener('click', function (e) {
      if (poTahu) { poTahu = false; return; }
      if (e.target === dlg || e.target === figura) dlg.close();
    });
    // potiahnutie prstom (aj perom a myšou): vodorovne aspoň 48 px a výrazne viac než zvisle; pri priblížení
    // stránky (pinch zoom) sa neposúva, aby sa dalo prezerať priblíženú fotku
    if (figura && 'PointerEvent' in window) {
      figura.addEventListener('pointerdown', function (e) {
        tah = e.isPrimary ? { x: e.clientX, y: e.clientY, id: e.pointerId } : null;
      });
      figura.addEventListener('pointerup', function (e) {
        if (!tah || e.pointerId !== tah.id) return;
        var dx = e.clientX - tah.x;
        var dy = e.clientY - tah.y;
        tah = null;
        var vv = window.visualViewport;
        if (n < 2 || (vv && vv.scale > 1.01)) return;
        if (Math.abs(dx) >= 48 && Math.abs(dx) > Math.abs(dy) * 1.5) {
          poTahu = true;
          ukaz(akt + (dx < 0 ? 1 : -1));
          window.setTimeout(function () { poTahu = false; }, 400);
        }
      });
      figura.addEventListener('pointercancel', function () { tah = null; });
    }
  }
})();
