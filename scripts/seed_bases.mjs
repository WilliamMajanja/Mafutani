#!/usr/bin/env node
/**
 * Seed the informational consolidation-base registry.
 *
 * These are real ports, airports, docks, SEZs, dry ports and logistics hubs
 * used as context for regional market pages. Coordinates are approximate
 * city/terminal centres. Informational only: not legal title, registry, or
 * an assertion of operating status.
 */
import dotenv from "dotenv";
dotenv.config({ path: ".env.local" });
import { neon } from "@neondatabase/serverless";

const url = process.env.DATABASE_URL;
if (!url) { console.error("DATABASE_URL is not set"); process.exit(1); }
const sql = neon(url);

const BASES = [
  // Americas
  ["USNYNJ", "Port of New York & New Jersey", "seaport", "AMER", "United States", "New York / Newark", 40.668, -74.045, ["SPX", "JPM"], "Port Authority of NY & NJ", "Largest US East Coast container gateway."],
  ["USLAX", "Port of Los Angeles / Long Beach", "seaport", "AMER", "United States", "Los Angeles", 33.739, -118.261, ["SPX"], "Port of LA / Port of Long Beach", "Trans-Pacific container complex."],
  ["USCHI", "Chicago Rail & Intermodal Hub", "rail", "AMER", "United States", "Chicago", 41.878, -87.629, ["CORN", "US10Y"], "Class I railroads", "Major US rail and futures delivery hub."],
  ["USHOU", "Houston Ship Channel", "seaport", "AMER", "United States", "Houston", 29.735, -95.266, ["WTI", "NATGAS"], "Port of Houston", "Energy and petrochemical export complex."],
  ["USDFW", "Dallas/Fort Worth International Airport", "airport", "AMER", "United States", "Dallas", 32.899, -97.040, ["AAPL", "AMZN"], "DFW Airport", "Major air cargo and logistics node."],
  ["USFTZ1", "New York Foreign-Trade Zone 1", "sez", "AMER", "United States", "New York", 40.712, -74.006, ["SPX", "JPM"], "US FTZ Board", "Duty-deferral zone; informational."],
  ["USMIA", "Miami Free Zone", "sez", "AMER", "United States", "Miami", 25.795, -80.307, ["AMZN"], "Miami Free Zone", "Trade and distribution zone for the Americas."],

  // Europe, Middle East and Africa
  ["NLRTM", "Port of Rotterdam", "seaport", "EMEA", "Netherlands", "Rotterdam", 51.949, 4.145, ["EURUSD", "WTI"], "Port of Rotterdam Authority", "Europe's largest seaport."],
  ["BEANR", "Port of Antwerp-Bruges", "seaport", "EMEA", "Belgium", "Antwerp", 51.260, 4.402, ["EURUSD"], "Port of Antwerp-Bruges", "Chemical and container gateway."],
  ["DEHAM", "Port of Hamburg", "seaport", "EMEA", "Germany", "Hamburg", 53.546, 9.968, ["EURUSD"], "Hamburg Port Authority", "North Sea and Baltic gateway."],
  ["GBFXT", "Port of Felixstowe", "seaport", "EMEA", "United Kingdom", "Felixstowe", 51.961, 1.351, ["GBPUSD"], "Port of Felixstowe", "UK's largest container port."],
  ["GBLHR", "Heathrow Airport", "airport", "EMEA", "United Kingdom", "London", 51.470, -0.454, ["GBPUSD"], "Heathrow Airport", "Major air cargo and passenger hub."],
  ["DEFRA", "Frankfurt Airport", "airport", "EMEA", "Germany", "Frankfurt", 50.037, 8.562, ["EURUSD"], "Fraport", "European air cargo hub."],
  ["KEMBA", "Port of Mombasa", "seaport", "EMEA", "Kenya", "Mombasa", -4.063, 39.668, [], "Kenya Ports Authority", "Gateway for East and Central Africa."],
  ["KENBO", "Naivasha Inland Container Depot", "icd", "EMEA", "Kenya", "Naivasha", -0.717, 36.431, [], "Kenya Railways", "Dry port serving the Northern Corridor."],
  ["KEDKU", "Dongo Kundu Special Economic Zone", "sez", "EMEA", "Kenya", "Mombasa", -4.017, 39.617, [], "Kenya SEZ Authority", "SEZ adjoining Mombasa port."],
  ["KENJK", "Jomo Kenyatta International Airport", "airport", "EMEA", "Kenya", "Nairobi", -1.319, 36.928, [], "Kenya Airports Authority", "East African air cargo hub."],
  ["AEJEA", "Jebel Ali Port & Free Zone", "seaport", "EMEA", "United Arab Emirates", "Dubai", 25.011, 55.061, [], "DP World", "Largest Middle East container port and free zone."],
  ["SAJED", "Jeddah Islamic Port", "seaport", "EMEA", "Saudi Arabia", "Jeddah", 21.485, 39.176, [], "Mawani", "Red Sea gateway."],
  ["ZADUR", "Port of Durban", "seaport", "EMEA", "South Africa", "Durban", -29.878, 31.045, [], "Transnet", "Southern African container hub."],

  // Asia-Pacific
  ["SGSIN", "Port of Singapore", "seaport", "APAC", "Singapore", "Singapore", 1.264, 103.840, ["BTCUSD", "ETHUSD"], "MPA Singapore", "World's largest transshipment port."],
  ["CNSHA", "Port of Shanghai", "seaport", "APAC", "China", "Shanghai", 31.230, 121.474, [], "Shanghai International Port Group", "World's busiest container port."],
  ["KRPUS", "Port of Busan", "seaport", "APAC", "South Korea", "Busan", 35.102, 129.040, [], "Busan Port Authority", "North Asian transshipment hub."],
  ["JPTYO", "Port of Tokyo / Haneda", "seaport", "APAC", "Japan", "Tokyo", 35.617, 139.793, ["N225"], "Port of Tokyo / Haneda", "Tokyo Bay port and airport complex."],
  ["HKKHH", "Port of Hong Kong", "seaport", "APAC", "Hong Kong", "Hong Kong", 22.304, 114.166, ["HSI"], "Marine Department", "Deep-water container port."],
  ["AUMEL", "Port of Melbourne", "seaport", "APAC", "Australia", "Melbourne", -37.829, 144.915, ["ASX200"], "Port of Melbourne", "Australia's largest container port."],
  ["INNSA", "Nhava Sheva (JNPT)", "seaport", "APAC", "India", "Mumbai", 18.949, 72.951, ["SENSEX"], "Jawaharlal Nehru Port Authority", "India's largest container port."],
  ["CNSEZ", "Shenzhen Special Economic Zone", "sez", "APAC", "China", "Shenzhen", 22.543, 114.058, [], "Shenzhen Municipal Government", "Manufacturing and export zone."],
  ["MYPKG", "Port Klang Free Zone", "sez", "APAC", "Malaysia", "Port Klang", 3.003, 101.395, [], "PKFZ", "Free zone adjoining Port Klang."],
];

async function main() {
  await sql`
    CREATE TABLE IF NOT EXISTS consolidation_bases (
      code TEXT PRIMARY KEY,
      name TEXT NOT NULL,
      type TEXT NOT NULL,
      region TEXT NOT NULL,
      country TEXT NOT NULL,
      city TEXT NOT NULL,
      lat DOUBLE PRECISION NOT NULL,
      lon DOUBLE PRECISION NOT NULL,
      related_symbols TEXT NOT NULL DEFAULT '[]',
      operator TEXT NOT NULL DEFAULT '',
      notes TEXT NOT NULL DEFAULT '',
      updated_at BIGINT NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT
    )
  `;
  await sql`CREATE INDEX IF NOT EXISTS idx_bases_region ON consolidation_bases(region, type)`;
  for (const b of BASES) {
    await sql`
      INSERT INTO consolidation_bases
        (code,name,type,region,country,city,lat,lon,related_symbols,operator,notes)
      VALUES (${b[0]},${b[1]},${b[2]},${b[3]},${b[4]},${b[5]},${b[6]},${b[7]},${JSON.stringify(b[8])},${b[9]},${b[10]})
      ON CONFLICT (code) DO UPDATE SET
        name=EXCLUDED.name, type=EXCLUDED.type, region=EXCLUDED.region,
        country=EXCLUDED.country, city=EXCLUDED.city, lat=EXCLUDED.lat, lon=EXCLUDED.lon,
        related_symbols=EXCLUDED.related_symbols, operator=EXCLUDED.operator,
        notes=EXCLUDED.notes, updated_at=EXTRACT(EPOCH FROM NOW())::BIGINT
    `;
  }
  console.log(`seeded ${BASES.length} consolidation bases`);
}

main().catch((e) => { console.error(e); process.exit(1); });
