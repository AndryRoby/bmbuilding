/* BM Building, skript webu: pretočenie číslic kót, jemné objavenie sekcií úvodu, mobilné menu (Esc, klik mimo,
   kotva), čiara pod pripnutou hlavičkou, slider stavieb v úvode, spodná lišta Zavolať a Napísať e-mail, filter
   a zoradenie realizácií, lightbox s viacerými galériami na stránke (markup skladá generátor webu).
   Prelínanie fotiek slidera a odkrytie fotky sú CSS. Pri spustení (bez kotvy v adrese) ani pri posune stránky sa tu
   nečíta rozloženie (žiadny vynútený prepočet): lištu, čiaru hlavičky, slider a objavenie sekcií prepínajú
   pozorovatelia (IntersectionObserver). Generátor webu súbor pri zostave zhustí (bez komentárov a odsadenia).
   Web ukáže všetok obsah aj bez tohto súboru. Žiadne údaje z obsahu sa nevkladajú ako HTML: len atribúty,
   textContent a presun existujúcich prvkov. */
(function () {
  'use strict';
  window.BMB = 1;
  var d = document;
  // trieda js (vložený skript v hlave ju dá pred prvým vykreslením a zruší pri DOMContentLoaded len vtedy, keď tento
  // súbor nepribehol, napr. chyba načítania; odložený skript sa spustí vždy pred DOMContentLoaded, aj na pomalej
  // sieti, doladenie-2); pridanie tu je len poistka, aby filter a ovládanie slidera nikdy neostali skryté
  d.documentElement.classList.add('js');
  var maIO = 'IntersectionObserver' in window;
  var pohyb = !(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  function vsetky(sel, koren) { return Array.prototype.slice.call((koren || d).querySelectorAll(sel)); }

  // 0. Číslice kót (počítadlo slidera, čísla Činnosti, detail) sa pri vstupe do obrazu raz pretočia o celé kolo (číslice dodá CSS,
  //    tu sa len pridá trieda). Pri obmedzenom pohybe sa nič nedeje, čísla ostanú, ako sú.
  if (pohyb && maIO) {
    var ioOtoc = new IntersectionObserver(function (zaznamy) {
      zaznamy.forEach(function (z) {
        if (!z.isIntersecting) return;
        z.target.classList.add('otocene');
        ioOtoc.unobserve(z.target);
      });
    }, { threshold: 0.6, rootMargin: '0px 0px -2% 0px' });
    vsetky('.otoc').forEach(function (el) { ioOtoc.observe(el); });
  }

  // 0b. Sekcie pod hrdinom úvodu (nadpis, karty Činnosti, veta SKSI a otázky zo zákona) sa jemne objavia pri vstupe do obrazu
  //     (pohybový jazyk pôvodného webu, ÚVOD v3). Trieda vstup len prvkom celých pod oknom (čo už človek vidí, sa
  //     neskryje), videne raz pri vstupe; CSS skrýva len pri triede js a bez obmedzeného pohybu, takže bez skriptu
  //     a pri obmedzenom pohybe je všetko hneď viditeľné. Kontakt sa neskrýva (modrý blok je vidno hneď, aj pri
  //     rýchlom posune). Fokus klávesnice v ešte skrytom bloku ho odkryje hneď (focusin): Chrome posunie stránku
  //     len tak, aby bol odkaz v okne, často do spodných 8 %, ktoré pozorovateľ nesleduje, a obrys fokusu by ostal
  //     neviditeľný (audit naplno 25. 11. 2026, položka 6, WCAG 2.4.7).
  //     Polohu bloku pri načítaní dá prvé hlásenie pozorovateľa ioPoloha (boundingClientRect a rootBounds z rozloženia,
  //     ktoré prehliadač aj tak počíta), nie innerHeight a getBoundingClientRect: tie pri spustení skriptu vynútili
  //     prepočet celého rozloženia, pri CPU 4x 270 až 313 ms (audit naplno, položka 48). Blok pod oknom sa tak skryje
  //     až po prvom vykreslení, keď ho človek ešte nevidí.
  if (pohyb && maIO && d.querySelector('.stranka-uvod')) {
    var ioVstup = new IntersectionObserver(function (zaznamy) {
      zaznamy.forEach(function (z) {
        if (!z.isIntersecting) return;
        z.target.classList.add('videne');
        ioVstup.unobserve(z.target);
      });
    }, { threshold: 0, rootMargin: '0px 0px -8% 0px' });
    // threshold 0: karta Činnosti je na mobile vysoká aj 2 000 px (19 bodov dozoru), pri podiele 0,12 by sa objavila
    // až po 240 px prázdneho miesta na spodku okna; teraz hneď, keď jej horný okraj prejde 8 % nad spodok okna
    var ioPoloha = new IntersectionObserver(function (zaznamy) {
      zaznamy.forEach(function (z) {
        ioPoloha.unobserve(z.target);
        // horný okraj na spodku okna alebo nižšie (celý blok pod oknom): skryť a čakať na vstup; čo už vidno, ostane
        if (!z.rootBounds || z.boundingClientRect.top < z.rootBounds.bottom) return;
        z.target.classList.add('vstup');
        ioVstup.observe(z.target);
      });
    }, { threshold: 0 });
    vsetky('.cinnost-sekcia .nadpis, .cinnost-karta, .cinnost-sksi, .zakon-list').forEach(function (el) {
      ioPoloha.observe(el);
    });
    d.addEventListener('focusin', function (e) {
      var v = e.target && e.target.closest ? e.target.closest('.vstup:not(.videne)') : null;
      if (!v) return;
      v.classList.add('videne');
      ioVstup.unobserve(v);
    });
  }

  // 1. Mobilné menu (details funguje aj bez skriptu): pri otvorení text Zavrieť, Esc zavrie a vráti ohnisko na
  //    tlačidlo, klik na kotvu na tej istej stránke alebo klik mimo panelu zavrie. Systémové Späť v Androide (aj gesto)
  //    zavrie otvorené menu cez CloseWatcher a nechá človeka na stránke (audit naplno 25. 11. 2026, položka 26: Späť
  //    pri otvorenom menu opustilo stránku); prehliadač bez CloseWatcher sa správa ako doteraz.
  vsetky('details.menu-m').forEach(function (menu) {
    var tl = menu.querySelector('summary');
    if (!tl) return;
    var povodny = tl.textContent;
    var strazca = null;
    function zavri(spat) {
      if (!menu.open) return;
      menu.open = false;
      if (spat) tl.focus();
    }
    menu.addEventListener('toggle', function () {
      tl.textContent = menu.open ? 'Zavrieť' : povodny;
      if (menu.open && !strazca && typeof CloseWatcher === 'function') {
        try {
          strazca = new CloseWatcher();
          strazca.onclose = function () { strazca = null; zavri(true); };
        } catch (e) { strazca = null; }
      } else if (!menu.open && strazca) {
        var s = strazca;
        strazca = null;
        s.destroy();
      }
    });
    // Esc zavrie otvorené menu aj vtedy, keď fokus nie je v ňom (ťuk na prázdnu plochu panela presunie fokus na body;
    // kontrola 3 v prehliadači, nález drobné), a vráti fokus na tlačidlo
    d.addEventListener('keydown', function (e) {
      if (menu.open && (e.key === 'Escape' || e.key === 'Esc')) { e.preventDefault(); zavri(true); }
    });
    // fokus odíde z menu na prvok mimo neho (Tab za posledným odkazom panela, Shift+Tab pred tlačidlom): menu sa zavrie,
    // aby fokus nebol pod otvoreným panelom (doladenie-3, overenie 3 nález drobný, WCAG 2.2 bod 2.4.11). Bez nového
    // prvku s fokusom (ťuk na plochu panela, odchod z okna, odkaz na iOS nedostane fokus) sa nič nedeje, ťuk na odkaz
    // v paneli tak vždy naviguje.
    menu.addEventListener('focusout', function (e) {
      var kam = e.relatedTarget;
      if (menu.open && kam && kam.nodeType === 1 && !menu.contains(kam)) zavri(false);
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

  // 3. Pripnutá hlavička: vlasová čiara pod ňou až po posune stránky (trieda posunute), na začiatku splýva s úvodom.
  //    Posun nad 4 px hlási pozorovateľ značky vysokej 4 px na vrchu stránky (aj prvým hlásením po príchode na kotvu
  //    alebo po obnovení v strede stránky), nie čítanie window.pageYOffset: to hneď pri spustení vynútilo prepočet
  //    celého rozloženia, na /realizacie/ a detaile pri CPU 4x 61 až 265 ms (audit naplno 25. 11. 2026, položka 48).
  //    Bez IntersectionObserver ako predtým cez posun, prvé čítanie až v requestAnimationFrame.
  (function () {
    var hlava = d.querySelector('.hlava');
    if (!hlava) return;
    if (maIO && d.body) {
      var znacka = d.createElement('div');
      znacka.setAttribute('aria-hidden', 'true');
      // CSSOM, nie atribút style (CSP); body má position relative, značka je mimo toku a bez kliknutí
      var st = znacka.style;
      st.position = 'absolute';
      st.top = '0';
      st.left = '0';
      st.width = '1px';
      st.height = '4px';
      st.pointerEvents = 'none';
      st.visibility = 'hidden';
      d.body.insertBefore(znacka, d.body.firstChild);
      new IntersectionObserver(function (zaznamy) {
        var z = zaznamy[zaznamy.length - 1];
        if (z) hlava.classList.toggle('posunute', !z.isIntersecting);
      }).observe(znacka);
      return;
    }
    var caka = false;
    var obnov = function () { caka = false; hlava.classList.toggle('posunute', (window.pageYOffset || 0) > 4); };
    window.addEventListener('scroll', function () {
      if (caka) return;
      caka = true;
      window.requestAnimationFrame(obnov);
    }, { passive: true });
    window.requestAnimationFrame(obnov);
  })();
  // Odložená fotka slidera: skutočný zdroj je v data-src a data-srcset, kým nepríde na rad (bez skriptu je
  //    viditeľná len prvá fotka). AVIF (data-avif): fotka sa obalí do picture so zdrojom
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

  // 3b. Slider stavieb v úvode (1. kolo pripomienok 24. 11. 2026, ZMENY-KOLO-1.md 3.3, markup build.py Web.slider):
  //     fotky sa prelínajú (CSS, data-prelinanie ms) a každá ostane data-interval ms. Sám sa zastaví pri myši nad
  //     sliderom, pri fokuse v ňom, pri skrytej karte prehliadača, mimo obrazu a tlačidlom Pozastaviť (aria-pressed);
  //     pri obmedzenom pohybe sa sám neposúva (tlačidlá áno, bez prelínania). Fotka sa načíta (data-src, data-srcset,
  //     data-avif) tesne pred prechodom a prejde sa na ňu až po načítaní. Bez skriptu prvá fotka s popisom.
  (function () {
    var slider = d.querySelector('.slider');
    if (!slider) return;
    var snimky = vsetky('.snimka', slider);
    var popisy = vsetky('.slider-popis', slider);
    var ovladanie = slider.querySelector('.slider-ovladanie');
    if (snimky.length < 2 || !ovladanie) return;
    var cislo = slider.querySelector('.slider-cislo');
    var zoznamPopisov = slider.querySelector('.slider-popisy');
    var interval = Number(slider.getAttribute('data-interval')) || 8000;
    var prelinanie = Number(slider.getAttribute('data-prelinanie')) || 1200;
    var NAPRED = 2500;
    var i = 0;
    var poziadavka = 0;
    var casovac = null;
    var casNapred = null;
    var dovody = { tlacidlo: !pohyb };
    // CSSOM (nie atribút style): CSP webu atribút style nedovolí
    slider.style.setProperty('--prelinanie', (pohyb ? prelinanie : 0) + 'ms');
    var pauza = slider.querySelector('[data-slider="pauza"]');
    if (pauza) pauza.setAttribute('aria-pressed', dovody.tlacidlo ? 'true' : 'false');

    // počítadlo: mení sa len skrytý text pre čítačku a číslica každého valca (trieda c0 až c9), markup kóty ostane,
    // takže každé číslo má to isté písmo a šírku ako prvé 01 a číslica sa pretočí o krok (overenie-1, nález drobné:
    // textContent zrušil valce a ďalšie čísla boli obyčajný text)
    function nastavCislo(n) {
      if (!cislo) return;
      var text = (n < 10 ? '0' : '') + n;
      var vh = cislo.querySelector('.vh');
      var stlpce = cislo.querySelectorAll('.stlpec');
      if (!vh || stlpce.length !== text.length) { cislo.textContent = text; return; }
      vh.textContent = text;
      for (var k = 0; k < stlpce.length; k++) {
        stlpce[k].className = stlpce[k].className.replace(/\bc\d\b/, 'c' + text.charAt(k));
      }
    }
    function obrazok(j) { return snimky[j].querySelector('img'); }
    function priprav(j) {
      var img = obrazok(j);
      if (img && img.hasAttribute('data-src')) nacitaj(img);
    }
    // prejde sa až na načítanú a dekódovanú fotku (img.decode): pri obmedzenom pohybe sa stará fotka skryje hneď
    // a nová by sa na zlomok sekundy kreslila prázdna (doladenie-1, nález drobné); chyba alebo 4 s prejdú aj tak
    function kedNacitana(j, fn) {
      var img = obrazok(j);
      var hotovo = false;
      var raz = function () { if (!hotovo) { hotovo = true; fn(); } };
      var dekoduj = function () {
        if (img && typeof img.decode === 'function') img.decode().then(raz, raz); else raz();
      };
      if (!img || (img.complete && img.naturalWidth > 0)) { dekoduj(); return; }
      img.addEventListener('load', dekoduj, { once: true });
      img.addEventListener('error', raz, { once: true });
      window.setTimeout(raz, 4000);
    }
    function zastavene() {
      for (var k in dovody) if (dovody[k]) return true;
      return false;
    }
    function planuj() {
      window.clearTimeout(casovac);
      window.clearTimeout(casNapred);
      if (zastavene()) return;
      var dalsia = (i + 1) % snimky.length;
      casNapred = window.setTimeout(function () { priprav(dalsia); }, Math.max(0, interval - NAPRED));
      casovac = window.setTimeout(function () { ukaz(dalsia, false); }, interval);
    }
    function ukaz(j, rucne) {
      j = (j + snimky.length) % snimky.length;
      if (j === i) return;
      var moja = ++poziadavka;
      if (rucne && zoznamPopisov) zoznamPopisov.setAttribute('aria-live', 'polite');
      priprav(j);
      kedNacitana(j, function () {
        if (moja !== poziadavka) return;
        var stara = snimky[i];
        // nová fotka sa prelína nad starou, stará ostane plná, kým nová nedobehne (bez preblesku podkladu)
        stara.classList.add('bola');
        stara.classList.remove('je');
        window.setTimeout(function () { stara.classList.remove('bola'); }, pohyb ? prelinanie + 50 : 0);
        if (popisy[i]) popisy[i].classList.remove('je');
        snimky[j].classList.add('je');
        if (popisy[j]) popisy[j].classList.add('je');
        i = j;
        nastavCislo(i + 1);
        // po ručnom prepnutí obe susedné fotky vopred (Ďalšia aj Predchádzajúca)
        if (rucne) { priprav((i + 1) % snimky.length); priprav((i - 1 + snimky.length) % snimky.length); }
        planuj();
      });
    }
    function nastav(dovod, hodnota) {
      if (!!dovody[dovod] === !!hodnota) return;
      dovody[dovod] = hodnota;
      planuj();
    }
    slider.addEventListener('mouseenter', function () { nastav('mys', true); });
    slider.addEventListener('mouseleave', function () { nastav('mys', false); });
    slider.addEventListener('focusin', function () { nastav('fokus', true); });
    slider.addEventListener('focusout', function (e) {
      if (!e.relatedTarget || !slider.contains(e.relatedTarget)) nastav('fokus', false);
    });
    d.addEventListener('visibilitychange', function () { nastav('karta', d.hidden); });
    if (maIO) {
      new IntersectionObserver(function (zaznamy) {
        var z = zaznamy[zaznamy.length - 1];
        if (z) nastav('obraz', !z.isIntersecting);
      }, { threshold: 0.25 }).observe(slider);
    }
    vsetky('[data-slider]', ovladanie).forEach(function (b) {
      b.addEventListener('click', function () {
        var smer = b.getAttribute('data-slider');
        if (smer === 'pauza') {
          var stoj = b.getAttribute('aria-pressed') !== 'true';
          b.setAttribute('aria-pressed', stoj ? 'true' : 'false');
          nastav('tlacidlo', stoj);
          if (!stoj) nastav('tah', false);
          return;
        }
        ukaz(i + Number(smer), true);
      });
    });
    // Potiahnutie prstom alebo perom po fotke (myš má šípky a pauzu pri prejdení): vodorovne aspoň 48 px a viac ako
    // 1,5-násobok zvislého pohybu ako v lightboxe, prepne ako šípky. Slider potom stojí ako po šípke (tá drží fokus
    // v slideri), kým sa človek nedotkne miesta mimo neho alebo nepustí prehrávanie tlačidlom; pri priblížení stránky
    // sa neposúva. Zvislý pohyb posúva stránku (styl.css touch-action pan-y na .snimky). Audit naplno 25. 11. 2026,
    // položka 11: potiahnutie stavbu nemenilo.
    var plocha = slider.querySelector('.snimky');
    if (plocha && 'PointerEvent' in window) {
      var tahS = null;
      plocha.addEventListener('pointerdown', function (e) {
        tahS = e.isPrimary && e.pointerType !== 'mouse' ? { x: e.clientX, y: e.clientY, id: e.pointerId } : null;
      });
      plocha.addEventListener('pointerup', function (e) {
        if (!tahS || e.pointerId !== tahS.id) return;
        var dx = e.clientX - tahS.x;
        var dy = e.clientY - tahS.y;
        tahS = null;
        var vv = window.visualViewport;
        if (vv && vv.scale > 1.01) return;
        if (Math.abs(dx) >= 48 && Math.abs(dx) > Math.abs(dy) * 1.5) {
          nastav('tah', true);
          ukaz(i + (dx < 0 ? 1 : -1), true);
        }
      });
      plocha.addEventListener('pointercancel', function () { tahS = null; });
      d.addEventListener('pointerdown', function (e) {
        if (dovody.tah && !slider.contains(e.target)) nastav('tah', false);
      }, true);
    }
    planuj();
    // pri obmedzenom pohybe sa slider sám neposúva a planuj nič nepripraví: susedné fotky sa načítajú hneď po načítaní
    // stránky, aby prvé stlačenie Ďalšia alebo Predchádzajúca nečakalo na sťahovanie (doladenie-1)
    if (!pohyb) {
      var susedne = function () { priprav(1); priprav(snimky.length - 1); };
      if (d.readyState === 'complete') susedne(); else window.addEventListener('load', susedne, { once: true });
    }
  })();

  // 4b. Kotva z inej stránky (menu Činnosť z podstránky, napríklad ../#cinnost): písma a fotky môžu zmeniť výšku
  //     úvodu až po prvom posune prehliadača na kotvu. Po zostavení
  //     stránky, po načítaní a po písmach sa znova postaví na cieľ kotvy, kým sa sám nepohol.
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
    // po načítaní a po písmach a ešte raz o chvíľu, keby prehliadač dobiehal vlastný posun
    var neskor = function () { window.requestAnimationFrame(skoc); window.setTimeout(skoc, 150); window.setTimeout(skoc, 600); };
    neskor();
    if (d.readyState !== 'complete') window.addEventListener('load', neskor);
    if (d.fonts && d.fonts.ready) d.fonts.ready.then(neskor);
    // každá zmena výšky stránky v prvých 8 sekundách postaví stránku znova na kotvu, kým sa človek sám nepohol
    if (typeof ResizeObserver === 'function') {
      var sledovanie = new ResizeObserver(function () { if (!pohol) window.requestAnimationFrame(skoc); });
      sledovanie.observe(d.body);
      window.setTimeout(function () { sledovanie.disconnect(); }, 8000);
    }
  })();
  // Plynulý posun (CSS html.plynuly-posun) až po načítaní stránky, aby príchod s kotvou skočil hneď na miesto.
  (function () {
    var zapni = function () { window.setTimeout(function () { d.documentElement.classList.add('plynuly-posun'); }, 700); };
    if (d.readyState === 'complete') zapni(); else window.addEventListener('load', zapni);
  })();

  // 5. Filter, zoradenie a postupné načítanie realizácií: stav v adrese ?druh=&roky=&mesto=&zoradit=, počet
  //    v aria-live, prázdny výsledok. Všetky karty sú v HTML (bez skriptu viditeľné všetky); filter a zoradenie
  //    idú cez všetky karty, z výsledku sa ukáže prvých data-naraz a tlačidlo Zobraziť ďalšie s počtom zvyšných
  //    (zvyšok najviac polovica dávky sa ukáže hneď s dávkou). Po kliknutí ide fokus na nadpis prvej novej karty
  //    (klávesnica pokračuje tam, kde skončila) a karta sa posunie do okna.
  var filtre = d.querySelector('[data-filtre]');
  var zoznam = d.querySelector('[data-zoznam]');
  if (filtre && zoznam) {
    var dlazdice = vsetky('.dlazdica', zoznam);
    // karty nad prvou dávkou majú z build.py data-neskor a styl.css ich do spustenia tohto skriptu necháva mimo
    // rozloženia, aby sa ich lenivé fotky nesťahovali (audit naplno 25. 11. 2026, položka 46: pri 1440 px a Slow 4G
    // 8 fotiek, 357 kB). Atribút sa zmaže hneď a obnov nižšie v tej istej úlohe (bez vykreslenia medzi tým) skryje
    // karty mimo výberu atribútom hidden; keby niečo ďalej zlyhalo, karty ostanú viditeľné ako bez skriptu.
    dlazdice.forEach(function (li) { li.removeAttribute('data-neskor'); });
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
    // priblíži k tlačidlu, aby sa po kliknutí neobjavovali postupne. Obrázky v skrytej karte s loading="eager" prehliadač stiahne, s lazy nie.
    var dalsiaDavka = [];
    var predNacitaj = function () {
      dalsiaDavka.forEach(function (li) {
        vsetky('img[loading="lazy"]', li).forEach(function (img) { img.setAttribute('loading', 'eager'); });
      });
    };
    // koľko kariet z n vybraných ukázať pri kolko: malý zvyšok (najviac polovica dávky) ide naraz s dávkou, aby za
    // posledným kliknutím neostali dve karty („Zobraziť ďalšie (14)“ ukázalo 12 a nechalo „(2)“, audit naplno
    // 25. 11. 2026, položka 17); build.py stav_zoznamu ráta rovnako
    var hranica = function (pocet, kolko) { return !naraz || pocet - kolko <= Math.floor(naraz / 2) ? pocet : kolko; };
    var obnov = function (zapis, novaPrva) {
      var prvaNova = null;
      var poradie = dlazdice.slice().sort(porovnaj);
      var vyhovuje = poradie.map(function (li) {
        var druhy = ' ' + (li.getAttribute('data-druh') || '') + ' ';
        var rok = Number(li.getAttribute('data-rok')) || 0;
        var dekada = rok ? String(rok - rok % 10) : '0';
        return (!s.druh || druhy.indexOf(' ' + s.druh + ' ') >= 0) && (!s.roky || s.roky === dekada)
          && (!s.mesto || li.getAttribute('data-mesto') === s.mesto);
      });
      var n = vyhovuje.filter(function (x) { return x; }).length;
      var m = hranica(n, ukazat);
      var dalej = hranica(n, ukazat + naraz);
      var k = 0;
      dalsiaDavka = [];
      poradie.forEach(function (li, j) {
        var ok = vyhovuje[j];
        var vidno = ok && k < m;
        if (ok && !vidno && k < dalej) dalsiaDavka.push(li);
        if (vidno && novaPrva !== undefined && k === novaPrva) prvaNova = li;
        if (ok) k++;
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
        // preventScroll: bez neho prehliadač posunul stránku k nadpisu prvej novej karty (pri 1600 px o 798 px).
        // Nové karty sa ukážu tam, kde bolo tlačidlo, fokus ide za nimi; prvá nová karta sa potom posunie celá do okna
        // najkratšou cestou (block nearest; audit naplno 25. 11. 2026, položka 36: nadpis s fokusom ostal pod oknom).
        // Do 767 px drží scroll-padding-bottom html kartu nad spodnou lištou.
        if (odkaz) odkaz.focus({ preventScroll: true });
        if (typeof prvaNova.scrollIntoView === 'function') prvaNova.scrollIntoView({ block: 'nearest' });
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
    // Zrušiť filter je v prázdnom výsledku, ktorý sa ním skryje: fokus ide na tlačidlo Všetky, nie na body (oprava po
    // overení, kontrola v prehliadači: čítačka stratila miesto a ďalší Tab išiel až na Kontakt za zoznamom, WCAG 2.4.3)
    if (zrusit) zrusit.addEventListener('click', function () {
      s.druh = ''; s.roky = ''; s.mesto = ''; zmena();
      var vsetkyDruhy = filtre.querySelector('[data-druh=""]');
      if (vsetkyDruhy) vsetkyDruhy.focus();
    });
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
    // filter a riadok stavu sú v rozložení od prvého vykreslenia (bez JS ich skryje styl.css html:not(.js)), app.js
    // len nastaví stav z adresy; bez filtra v adrese sa nič neposunie (doladenie-1, CLS pri pomalej sieti)
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
        foto.setAttribute('width', sirka ? String(sirka) : '');
        foto.setAttribute('height', a.getAttribute('data-vyska') || '');
        var av = avifZ(a);
        if (lbAvif) { if (av) lbAvif.setAttribute('srcset', av); else lbAvif.removeAttribute('srcset'); }
        foto.setAttribute('srcset', srcsetZ(a));
        foto.setAttribute('src', plnaZ(a));
        // eager až po novom zdroji: prepnutie z lazy pred zmenou zdroja by stiahlo zástupnú fotku dialógu (doladenie-2,
        // overenie 2: pri prvom otvorení navyše asi 207 kB, ktoré sa nikdy neukázali)
        foto.setAttribute('loading', 'eager');
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
