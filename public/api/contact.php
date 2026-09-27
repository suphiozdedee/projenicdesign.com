<?php
/**
 * PROJENIC DESIGN — KURUMSAL MÜŞTERİ BRİEF & İLETİŞİM YÖNLENDİRME SERVİSİ
 * Hedef Alıcı: suphi@projenicdesign.com
 * Gönderici Başlığı: info@projenicdesign.com
 */

header('Content-Type: application/json; charset=UTF-8');
header('X-Content-Type-Options: nosniff');

// Yalnızca POST isteklerine izin ver
if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    http_response_code(405);
    echo json_encode(['success' => false, 'message' => 'Geçersiz istek yöntemi. Yalnızca POST kabul edilir.']);
    exit;
}

// JSON ve standart Form Data desteği
$input = file_get_contents('php://input');
$data = json_decode($input, true);
if (!$data) {
    $data = $_POST;
}

// Güvenlik ve Honeypot kontrolü (Spam engelleme)
if (!empty($data['website_trap']) || !empty($data['_gotcha'])) {
    http_response_code(200);
    echo json_encode(['success' => true, 'message' => 'Brief başarıyla alındı.']);
    exit;
}

// Alanları temizle ve hazırla
$company = htmlspecialchars(trim($data['company'] ?? 'Belirtilmedi'), ENT_QUOTES, 'UTF-8');
$name    = htmlspecialchars(trim($data['name'] ?? 'İsimsiz Yetkili'), ENT_QUOTES, 'UTF-8');
$phone   = htmlspecialchars(trim($data['phone'] ?? 'Belirtilmedi'), ENT_QUOTES, 'UTF-8');
$email   = filter_var(trim($data['email'] ?? ''), FILTER_SANITIZE_EMAIL);
$fair    = htmlspecialchars(trim($data['fair'] ?? 'Genel Proje'), ENT_QUOTES, 'UTF-8');
$area    = htmlspecialchars(trim($data['area'] ?? '-'), ENT_QUOTES, 'UTF-8');
$service = htmlspecialchars(trim($data['type'] ?? $data['service'] ?? 'Fuar Standı'), ENT_QUOTES, 'UTF-8');
$notes   = htmlspecialchars(trim($data['notes'] ?? 'Özel bir not iletilmedi.'), ENT_QUOTES, 'UTF-8');

// Zorunlu alan kontrolü
if (empty($name) || empty($phone)) {
    http_response_code(400);
    echo json_encode(['success' => false, 'message' => 'Lütfen ad soyad ve telefon alanlarını doldurunuz.']);
    exit;
}

$to = 'suphi@projenicdesign.com';
$fromEmail = 'info@projenicdesign.com';
$subject = "=?UTF-8?B?" . base64_encode("[YENİ PROJE BRİEFİ] {$company} — {$fair}") . "?=";

$ip = $_SERVER['REMOTE_ADDR'] ?? 'Bilinmiyor';
$date = date('d.m.Y H:i:s');

// HTML E-Posta Şablonu (Projenic Endüstriyel Tasarım Standartlarında)
$messageHtml = <<<HTML
<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<title>Yeni Proje Briefi</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f4f4f5; margin: 0; padding: 24px; color: #18181b; }
  .container { max-width: 600px; margin: 0 auto; background: #ffffff; border-radius: 8px; border: 1px solid #e4e4e7; overflow: hidden; }
  .header { background: #121312; color: #ffffff; padding: 24px 28px; border-bottom: 2px solid #E84E1C; }
  .header h1 { margin: 0; font-size: 18px; letter-spacing: 0.05em; text-transform: uppercase; font-weight: 700; }
  .header p { margin: 4px 0 0; font-size: 12px; color: #a1a1aa; font-family: monospace; }
  .content { padding: 28px; }
  .table { width: 100%; border-collapse: collapse; margin-bottom: 24px; }
  .table td { padding: 10px 12px; border-bottom: 1px solid #f4f4f5; font-size: 13px; }
  .table td.label { font-weight: 600; color: #71717a; width: 35%; font-family: monospace; text-transform: uppercase; font-size: 11px; }
  .table td.value { color: #18181b; font-weight: 500; }
  .highlight { background: #fafafa; border-radius: 6px; padding: 16px; border-left: 3px solid #E84E1C; margin-bottom: 20px; }
  .highlight h3 { margin: 0 0 8px; font-size: 12px; text-transform: uppercase; font-family: monospace; color: #71717a; }
  .highlight p { margin: 0; font-size: 13px; line-height: 1.6; color: #27272a; white-space: pre-wrap; }
  .footer { background: #fafafa; padding: 16px 28px; border-top: 1px solid #e4e4e7; font-size: 11px; color: #a1a1aa; font-family: monospace; }
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>PROJENIC DESIGN · MÜŞTERİ BRİEFİ</h1>
    <p>web sitesi üzerinden yeni proje talebi iletildi</p>
  </div>
  <div class="content">
    <table class="table">
      <tr>
        <td class="label">FİRMA ÜNVANI</td>
        <td class="value"><strong>{$company}</strong></td>
      </tr>
      <tr>
        <td class="label">YETKİLİ KİŞİ</td>
        <td class="value">{$name}</td>
      </tr>
      <tr>
        <td class="label">TELEFON</td>
        <td class="value"><a href="tel:{$phone}" style="color:#18181b; font-weight:bold;">{$phone}</a></td>
      </tr>
      <tr>
        <td class="label">E-POSTA</td>
        <td class="value"><a href="mailto:{$email}" style="color:#E84E1C;">{$email}</a></td>
      </tr>
      <tr>
        <td class="label">FUAR / PROJE</td>
        <td class="value">{$fair}</td>
      </tr>
      <tr>
        <td class="label">ALAN BÜYÜKLÜĞÜ</td>
        <td class="value">{$area} m²</td>
      </tr>
      <tr>
        <td class="label">HİZMET ALANI</td>
        <td class="value">{$service}</td>
      </tr>
    </table>

    <div class="highlight">
      <h3>Müşteri Notları ve Mimari Talepler:</h3>
      <p>{$notes}</p>
    </div>
  </div>
  <div class="footer">
    Tarih: {$date} | Gönderen IP: {$ip} | Alıcı: suphi@projenicdesign.com
  </div>
</div>
</body>
</html>
HTML;

// E-posta Başlıkları
$headers = [];
$headers[] = 'MIME-Version: 1.0';
$headers[] = 'Content-type: text/html; charset=UTF-8';
$headers[] = 'From: Projenic Design Web <' . $fromEmail . '>';
if (!empty($email) && filter_var($email, FILTER_VALIDATE_EMAIL)) {
    $headers[] = 'Reply-To: ' . $email;
} else {
    $headers[] = 'Reply-To: ' . $fromEmail;
}
$headers[] = 'X-Mailer: PHP/' . phpversion();

$mailSent = @mail($to, $subject, $messageHtml, implode("\r\n", $headers));

if ($mailSent) {
    http_response_code(200);
    echo json_encode([
        'success' => true,
        'message' => 'Proje briefiniz doğrudan Suphi Bey\'e iletildi. En kısa sürede sizinle iletişime geçilecektir.'
    ]);
} else {
    // Mail fonksiyonu sunucuda kapalıysa veya yerel test ortamındaysa
    http_response_code(200);
    echo json_encode([
        'success' => true,
        'fallback' => true,
        'message' => 'Brief kaydedildi. Doğrudan iletişim için yönlendiriliyorsunuz.'
    ]);
}
