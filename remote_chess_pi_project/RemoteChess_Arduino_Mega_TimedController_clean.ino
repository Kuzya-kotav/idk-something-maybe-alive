// Remote Chess Arduino Mega controller
// Commands: PING, ZERO, POS, MOVE E2 E4, GOTO E4, MAG 1/0, JOG X 1, STOP

struct MotorPins {
  byte a;
  byte b;
  byte c;
  byte d;
};

struct Motor {
  MotorPins pins;
  int phase;
};

struct AxisPlan {
  int leftSign;
  int rightSign;
  unsigned long cellMs;
};

struct BoardPos {
  int x;
  int y;
  bool known;
};

const MotorPins LEFT_PINS  = {47, 49, 51, 53};
const MotorPins RIGHT_PINS = {46, 48, 50, 52};

const byte MAGNET_PIN = 7;
const byte RELAY_ON   = LOW;
const byte RELAY_OFF  = HIGH;

const unsigned long X_CELL_TIME_MS = 4000;
const unsigned long Y_CELL_TIME_MS = 4000;
const unsigned int MOTOR_PHASE_DELAY_US = 1500;

const unsigned long PICKUP_MS = 300;
const unsigned long DROP_MS   = 300;

const bool BOOT_AT_A1 = true;
const bool MOVE_X_FIRST = true;
const bool COILS_OFF_AFTER_AXIS = true;

const bool INVERT_BOARD_X = false;
const bool INVERT_BOARD_Y = false;

AxisPlan X_AXIS = {+1, +1, X_CELL_TIME_MS};
AxisPlan Y_AXIS = {+1, -1, Y_CELL_TIME_MS};

const byte PHASES[4][4] = {
  {HIGH, LOW,  HIGH, LOW},
  {LOW,  HIGH, HIGH, LOW},
  {LOW,  HIGH, LOW,  HIGH},
  {HIGH, LOW,  LOW,  HIGH}
};

Motor leftMotor  = {LEFT_PINS, 0};
Motor rightMotor = {RIGHT_PINS, 0};
BoardPos pos = {0, 0, BOOT_AT_A1};

String rx;
bool stopped = false;

void handle(String line);
void commandMove(String fromSq, String toSq);
void commandGoto(String sq);
void commandJog(String axis, String cellsText);
bool ready();
bool goTo(int x, int y);
bool moveAxis(char axis, int cells);
bool runFor(int leftDir, int rightDir, unsigned long ms);
bool stopWaiting();
void lostPosition();
void setMagnet(String value);
void magnet(bool on);
void motorInit(Motor &m);
void motorStep(Motor &m, int dir);
void motorWrite(Motor &m, int p);
void motorOff(Motor &m);
void motorsOff();
void allStop();
bool square(String sq, int &x, int &y);
void printPos();
void printSquare(int x, int y);
bool readLine(String &line);
String arg(String s, int index);

void setup() {
  Serial.begin(115200);

  motorInit(leftMotor);
  motorInit(rightMotor);

  pinMode(MAGNET_PIN, OUTPUT);
  allStop();

  Serial.println("OK BOOT REMOTE_CHESS_TIMED_CTRL");
}

void loop() {
  String line;
  if (readLine(line)) handle(line);
}

void handle(String line) {
  line.trim();
  line.toUpperCase();
  if (line.length() == 0) return;

  String cmd = arg(line, 0);

  if (cmd == "PING") {
    Serial.println("OK PONG");
    return;
  }

  if (cmd == "ZERO") {
    pos.x = 0;
    pos.y = 0;
    pos.known = true;
    stopped = false;
    allStop();
    Serial.println("OK ZERO A1");
    return;
  }

  if (cmd == "POS") {
    printPos();
    return;
  }

  if (cmd == "STOP") {
    stopped = true;
    allStop();
    Serial.println("OK STOP");
    return;
  }

  if (cmd == "MAG") {
    setMagnet(arg(line, 1));
    return;
  }

  if (cmd == "GOTO") {
    commandGoto(arg(line, 1));
    return;
  }

  if (cmd == "MOVE") {
    commandMove(arg(line, 1), arg(line, 2));
    return;
  }

  if (cmd == "JOG") {
    commandJog(arg(line, 1), arg(line, 2));
    return;
  }

  if (cmd == "CAPTURE") {
    Serial.println("OK CAPTURE IGNORED");
    return;
  }

  Serial.print("ERR UNKNOWN ");
  Serial.println(line);
}

void commandMove(String fromSq, String toSq) {
  int fx, fy, tx, ty;

  if (!ready()) return;
  if (!square(fromSq, fx, fy) || !square(toSq, tx, ty)) {
    Serial.println("ERR BAD_MOVE");
    return;
  }

  Serial.print("OK START MOVE ");
  Serial.print(fromSq);
  Serial.print(' ');
  Serial.println(toSq);

  stopped = false;

  if (!goTo(fx, fy)) return;

  magnet(true);
  delay(PICKUP_MS);

  if (!goTo(tx, ty)) {
    magnet(false);
    return;
  }

  magnet(false);
  delay(DROP_MS);

  Serial.print("OK DONE MOVE ");
  Serial.print(fromSq);
  Serial.print(' ');
  Serial.print(toSq);
  Serial.print(" POS ");
  printSquare(pos.x, pos.y);
  Serial.println();
}

void commandGoto(String sq) {
  int x, y;

  if (!ready()) return;
  if (!square(sq, x, y)) {
    Serial.println("ERR BAD_SQUARE");
    return;
  }

  Serial.print("OK START GOTO ");
  Serial.println(sq);

  stopped = false;
  if (!goTo(x, y)) return;

  Serial.print("OK DONE GOTO ");
  Serial.print(sq);
  Serial.print(" POS ");
  printSquare(pos.x, pos.y);
  Serial.println();
}

void commandJog(String axis, String cellsText) {
  axis.toUpperCase();
  int cells = cellsText.toInt();

  if (axis != "X" && axis != "Y") {
    Serial.println("ERR BAD_JOG_AXIS");
    return;
  }

  stopped = false;
  Serial.print("OK START JOG ");
  Serial.print(axis);
  Serial.print(' ');
  Serial.println(cells);

  if (moveAxis(axis[0], cells)) Serial.println("OK DONE JOG");
}

bool ready() {
  if (pos.known) return true;
  Serial.println("ERR NEED_ZERO");
  return false;
}

bool goTo(int x, int y) {
  int dx = x - pos.x;
  int dy = y - pos.y;

  if (MOVE_X_FIRST) {
    if (!moveAxis('X', dx)) return false;
    pos.x = x;
    if (!moveAxis('Y', dy)) return false;
    pos.y = y;
  } else {
    if (!moveAxis('Y', dy)) return false;
    pos.y = y;
    if (!moveAxis('X', dx)) return false;
    pos.x = x;
  }

  return true;
}

bool moveAxis(char axis, int cells) {
  if (cells == 0) return true;

  int sign = cells > 0 ? +1 : -1;
  int count = abs(cells);

  AxisPlan plan = axis == 'X' ? X_AXIS : Y_AXIS;

  int leftDir = sign * plan.leftSign;
  int rightDir = sign * plan.rightSign;
  unsigned long runMs = (unsigned long)count * plan.cellMs;

  return runFor(leftDir, rightDir, runMs);
}

bool runFor(int leftDir, int rightDir, unsigned long ms) {
  unsigned long t0 = millis();

  while ((unsigned long)(millis() - t0) < ms) {
    if (stopped || stopWaiting()) {
      lostPosition();
      return false;
    }

    motorStep(leftMotor, leftDir);
    motorStep(rightMotor, rightDir);
    delayMicroseconds(MOTOR_PHASE_DELAY_US);
  }

  if (COILS_OFF_AFTER_AXIS) motorsOff();
  return true;
}

bool stopWaiting() {
  String line;
  if (!readLine(line)) return false;

  line.trim();
  line.toUpperCase();

  if (line == "STOP") {
    stopped = true;
    return true;
  }

  Serial.print("BUSY IGNORED ");
  Serial.println(line);
  return false;
}

void lostPosition() {
  allStop();
  pos.known = false;
  Serial.println("ERR STOPPED NEED_ZERO");
}

void setMagnet(String value) {
  value.toUpperCase();

  if (value == "1" || value == "ON") {
    magnet(true);
    Serial.println("OK MAG 1");
    return;
  }

  if (value == "0" || value == "OFF") {
    magnet(false);
    Serial.println("OK MAG 0");
    return;
  }

  Serial.println("ERR BAD_MAG");
}

void magnet(bool on) {
  digitalWrite(MAGNET_PIN, on ? RELAY_ON : RELAY_OFF);
}

void motorInit(Motor &m) {
  pinMode(m.pins.a, OUTPUT);
  pinMode(m.pins.b, OUTPUT);
  pinMode(m.pins.c, OUTPUT);
  pinMode(m.pins.d, OUTPUT);
  motorOff(m);
}

void motorStep(Motor &m, int dir) {
  if (dir == 0) return;

  m.phase += dir;
  if (m.phase > 3) m.phase = 0;
  if (m.phase < 0) m.phase = 3;

  motorWrite(m, m.phase);
}

void motorWrite(Motor &m, int p) {
  digitalWrite(m.pins.a, PHASES[p][0]);
  digitalWrite(m.pins.b, PHASES[p][1]);
  digitalWrite(m.pins.c, PHASES[p][2]);
  digitalWrite(m.pins.d, PHASES[p][3]);
}

void motorOff(Motor &m) {
  digitalWrite(m.pins.a, LOW);
  digitalWrite(m.pins.b, LOW);
  digitalWrite(m.pins.c, LOW);
  digitalWrite(m.pins.d, LOW);
}

void motorsOff() {
  motorOff(leftMotor);
  motorOff(rightMotor);
}

void allStop() {
  motorsOff();
  magnet(false);
}

bool square(String sq, int &x, int &y) {
  sq.trim();
  sq.toUpperCase();

  if (sq.length() != 2) return false;
  if (sq[0] < 'A' || sq[0] > 'H') return false;
  if (sq[1] < '1' || sq[1] > '8') return false;

  x = sq[0] - 'A';
  y = sq[1] - '1';

  if (INVERT_BOARD_X) x = 7 - x;
  if (INVERT_BOARD_Y) y = 7 - y;

  return true;
}

void printPos() {
  if (!pos.known) {
    Serial.println("OK POS UNKNOWN");
    return;
  }

  Serial.print("OK POS ");
  printSquare(pos.x, pos.y);
  Serial.println();
}

void printSquare(int x, int y) {
  if (INVERT_BOARD_X) x = 7 - x;
  if (INVERT_BOARD_Y) y = 7 - y;

  Serial.print((char)('A' + x));
  Serial.print((char)('1' + y));
}

bool readLine(String &line) {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();

    if (c == '\r') continue;

    if (c == '\n') {
      line = rx;
      rx = "";
      line.trim();
      return true;
    }

    if (rx.length() < 80) {
      rx += c;
    } else {
      rx = "";
      line = "ERR_LINE_TOO_LONG";
      return true;
    }
  }

  return false;
}

String arg(String s, int index) {
  int found = 0;
  int start = -1;

  for (int i = 0; i <= s.length(); i++) {
    bool end = i == s.length();
    bool space = !end && (s[i] == ' ' || s[i] == '\t');

    if (!end && !space && start < 0) start = i;

    if ((end || space) && start >= 0) {
      if (found == index) return s.substring(start, i);
      found++;
      start = -1;
    }
  }

  return "";
}
