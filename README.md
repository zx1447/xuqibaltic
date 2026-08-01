# DigitalPlat Domain Auto Renew

自动续期 DigitalPlat 免费域名。每周检查，到期前 120 天自动续期。

## 配置

### Secrets
- `DIGITALPLAT_API_TOKEN` - DigitalPlat API token (dp_live_xxx)

### Variables (Settings → Actions → Variables)
- `DIGITALPLAT_DOMAINS` - 域名列表，一行一个

## API
- Base: `https://domain-api.digitalplat.org/api/v1`
- Auth: `Bearer dp_live_xxx`
- Renew: `POST /domains/{domain}/renew` body: `{"renewal_type":"free","years":1}`
