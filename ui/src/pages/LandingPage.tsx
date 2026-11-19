import { useEffect, type CSSProperties } from "react";
import { Link } from "react-router-dom";
import { Icon, type IconName } from "../components/Icon";
import { Logo } from "../components/Logo";
import "./landing.css";

// Isi dengan alamat repositori GitHub agar tombol kontribusi tampil. Kosong berarti tombol disembunyikan.
const REPO_URL = "";
const LOGIN = "/masuk";
const TITLE = "Nirflo - Riset kuantitatif saham Indonesia";

type Item = { icon: IconName; title: string; body: string };

const PRINCIPLES: readonly Item[] = [
  {
    icon: "zap",
    title: "Alat institusi, di tangan Anda",
    body: "Analisis kontribusi risiko, korelasi, dan Sharpe ratio dengan selang kepercayaan sering hanya ada di meja institusi. Nirflo membukanya untuk siapa saja yang ingin berinvestasi dengan lebih terukur.",
  },
  {
    icon: "chart",
    title: "Kuantitatif tanpa harus profesional",
    body: "Dirancang untuk apa pun latar belakang Anda. Setiap angka disertai penjelasan bahasa sehari-hari, sorotan otomatis, dan grafik yang bisa dibaca tanpa gelar statistik.",
  },
  {
    icon: "globe",
    title: "Bebas menggabungkan alat lain",
    body: "Tidak mengunci Anda. Tabel dapat diunduh sebagai CSV dan hasil evaluasi dapat diekspor ke JSON, sehingga mudah dipadukan dengan spreadsheet, notebook, atau alur riset yang sudah Anda punya.",
  },
  {
    icon: "search",
    title: "Transparan, dan Anda bisa ikut membangunnya",
    body: "Rumus, asumsi, dan batasan data dijelaskan di setiap hasil. Temukan celah, usulkan perbaikan, atau tambahkan alat baru bersama komunitas.",
  },
];

const FEATURES: readonly Item[] = [
  {
    icon: "chart",
    title: "Emiten",
    body: "Telusuri satu saham dari berbagai sisi, lengkap dengan pencarian cepat berdasarkan kode atau sektor.",
  },
  {
    icon: "zap",
    title: "Sinyal",
    body: "Pemindai sinyal kuantitatif aktif di seluruh pasar, diurutkan menurut kekuatan dan skor z.",
  },
  {
    icon: "pie",
    title: "Portofolio",
    body: "Bobot, kontribusi risiko, korelasi antar saham, dan Sharpe ratio sejak beli, bobot sekarang, serta sama rata.",
  },
  {
    icon: "globe",
    title: "Makro",
    body: "IHSG, kurs, komoditas, suku bunga, dan indikator Indonesia, bisa dibandingkan dan dilihat hubungannya.",
  },
];

const STEPS = [
  ["Masuk", "Gunakan akun Google, GitHub, atau email."],
  ["Isi posisi", "Masukkan kode saham, jumlah lot, dan tanggal beli."],
  ["Baca hasilnya", "Lihat risiko, korelasi, dan Sharpe ratio dengan penjelasan yang mudah dipahami."],
] as const;

const DEMO = [
  { name: "Saham A", w: 62, r: 78 },
  { name: "Saham B", w: 28, r: 16 },
  { name: "Saham C", w: 10, r: 6 },
] as const;

const TRANSPARENCY = [
  "Selang kepercayaan 95% ditampilkan, bukan satu angka tunggal.",
  "Catatan dan peringatan kualitas data ditampilkan, bukan disembunyikan.",
  "Return dihitung dari harga yang disesuaikan dividen dan split.",
  "Suku bunga bebas risiko memakai mata uang yang sama dengan return saham.",
] as const;

export default function LandingPage() {
  useEffect(() => {
    document.title = TITLE;
  }, []);

  return (
    <div className="lp">
      <header className="lp-nav">
        <div className="lp-wrap">
          <Link to="/" className="lp-brand" aria-label="Nirflo">
            <Logo size={32} />
            <span>Nirflo</span>
          </Link>
          <nav className="lp-links" aria-label="Bagian halaman">
            <a href="#prinsip">Prinsip</a>
            <a href="#fitur">Fitur</a>
            <a href="#mulai">Cara mulai</a>
            <a href="#terbuka">Terbuka</a>
          </nav>
          <Link to={LOGIN} className="lp-btn primary">Masuk</Link>
        </div>
      </header>

      <main>
        <section className="lp-hero">
          <div className="lp-wrap">
            <div>
              <span className="lp-eyebrow">Riset kuantitatif saham Indonesia</span>
              <h1>Alat riset institusi, <em>di tangan Anda.</em></h1>
              <p className="lp-lead">
                Nirflo membawa analisis kuantitatif yang biasanya hanya ada di meja institusi ke setiap investor
                Indonesia: risiko portofolio, sinyal statistik, dan konteks makro dalam satu terminal.
              </p>
              <div className="lp-cta">
                <Link to={LOGIN} className="lp-btn primary lg">Mulai sekarang <Icon name="arrow" size={18} /></Link>
                <a href="#prinsip" className="lp-btn lg">Pelajari prinsipnya</a>
              </div>
              <p className="lp-assure">Setiap hasil disertai selang kepercayaan dan catatan kualitas data.</p>
            </div>

            <aside className="lp-demo" aria-label="Contoh ilustrasi hasil analisis portofolio">
              <div className="lp-demo-head">
                <span>Portofolio contoh</span>
                <span className="lp-tag">Ilustrasi</span>
              </div>
              <div className="lp-sharpe">
                <div><small>Sharpe ratio</small><strong>0,42</strong></div>
                <span className="lp-chip">Belum pasti</span>
              </div>
              <div className="lp-legend" aria-hidden>
                <span><i className="sw w" />Bobot</span>
                <span><i className="sw r" />Kontribusi risiko</span>
              </div>
              <ul className="lp-bars">
                {DEMO.map((d) => (
                  <li key={d.name} className="lp-bar">
                    <b>{d.name}</b>
                    <span className="lp-tracks">
                      <span className="lp-t w"><i><b style={{ "--w": `${d.w}%` } as CSSProperties} /></i><span>{d.w}%</span></span>
                      <span className="lp-t r"><i><b style={{ "--w": `${d.r}%` } as CSSProperties} /></i><span>{d.r}%</span></span>
                    </span>
                  </li>
                ))}
              </ul>
              <p className="lp-note">Saham A hanya 62% dari bobot, tetapi menyumbang 78% dari risiko.</p>
              <p className="lp-fine">Data contoh untuk ilustrasi, bukan portofolio nyata.</p>
            </aside>
          </div>
        </section>

        <section id="prinsip" className="lp-sec">
          <div className="lp-wrap">
            <div className="lp-sec-head">
              <span className="lp-kicker">Prinsip</span>
              <h2>Riset yang seharusnya bisa dimiliki semua orang</h2>
              <p>Empat hal yang kami pegang dalam membangun Nirflo.</p>
            </div>
            <div className="lp-grid two">
              {PRINCIPLES.map((p) => (
                <article key={p.title} className="lp-card">
                  <span className="lp-ico"><Icon name={p.icon} size={20} /></span>
                  <h3>{p.title}</h3>
                  <p>{p.body}</p>
                </article>
              ))}
            </div>
          </div>
        </section>

        <section id="fitur" className="lp-sec">
          <div className="lp-wrap">
            <div className="lp-sec-head">
              <span className="lp-kicker">Fitur</span>
              <h2>Satu terminal, empat alat</h2>
              <p>Berpindah dari makro ke saham ke portofolio tanpa berpindah aplikasi.</p>
            </div>
            <div className="lp-grid four">
              {FEATURES.map((f) => (
                <article key={f.title} className="lp-card">
                  <span className="lp-ico"><Icon name={f.icon} size={20} /></span>
                  <h3>{f.title}</h3>
                  <p>{f.body}</p>
                </article>
              ))}
            </div>
          </div>
        </section>

        <section id="mulai" className="lp-sec">
          <div className="lp-wrap">
            <div className="lp-sec-head">
              <span className="lp-kicker">Cara mulai</span>
              <h2>Tiga langkah menuju gambaran risiko Anda</h2>
            </div>
            <ol className="lp-steps">
              {STEPS.map(([t, b]) => (
                <li key={t} className="lp-card">
                  <h3>{t}</h3>
                  <p>{b}</p>
                </li>
              ))}
            </ol>
          </div>
        </section>

        <section id="terbuka" className="lp-sec">
          <div className="lp-wrap">
            <div className="lp-open">
              <div>
                <span className="lp-kicker">Transparan dan terbuka</span>
                <h2>Anda boleh memeriksa, dan ikut membangunnya</h2>
                <p>
                  Kepercayaan datang dari kemampuan memeriksa. Karena itu cara hitung, asumsi, dan batasan data
                  ditampilkan apa adanya, dan pengembangan Nirflo terbuka untuk kontribusi.
                </p>
                <div className="lp-cta">
                  <Link to={LOGIN} className="lp-btn primary">Masuk</Link>
                  {REPO_URL && (
                    <a href={REPO_URL} className="lp-btn" target="_blank" rel="noopener noreferrer">Berkontribusi di GitHub</a>
                  )}
                </div>
              </div>
              <ul className="lp-checks">
                {TRANSPARENCY.map((t) => <li key={t}>{t}</li>)}
              </ul>
            </div>
          </div>
        </section>

        <section className="lp-final">
          <div className="lp-wrap">
            <Logo size={52} />
            <h2>Mulai membaca risiko portofolio Anda</h2>
            <p>Masuk dan tambahkan posisi pertama. Hasilnya langsung terlihat.</p>
            <Link to={LOGIN} className="lp-btn primary lg">Masuk ke Nirflo <Icon name="arrow" size={18} /></Link>
          </div>
        </section>
      </main>

      <footer className="lp-foot">
        <div className="lp-wrap">
          <div className="lp-foot-brand">
            <Logo size={26} />
            <span>Nirflo</span>
          </div>
          <p>
            Nirflo adalah alat riset dan edukasi, bukan nasihat investasi. Keputusan investasi sepenuhnya menjadi
            tanggung jawab Anda.
          </p>
          <small>&copy; {new Date().getFullYear()} Nirflo</small>
        </div>
      </footer>
    </div>
  );
}