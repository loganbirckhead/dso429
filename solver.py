"""Explicit, auditable calculations. No answer keys or external model calls."""
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
import re


class Unsolved(ValueError):
    """A question cannot be answered with a unique, verified choice."""


@dataclass(frozen=True)
class Solution:
    answer: str
    kind: str
    value: str
    formula: str


NUM = r"([0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)"
MONTHS = "January February March April May June July August September October November December".split()


def decimal(s):
    return Decimal(s.replace(",", ""))


def calculate(text):
    """Return (kind, value, explanation, unit) after matching the entire prompt."""
    def match(pattern):
        m = re.fullmatch(pattern, text.strip())
        return tuple(decimal(x) for x in m.groups()) if m else None

    with localcontext() as ctx:
        ctx.prec = 40
        if m := match(r"An investment of \$" + NUM + r" grows at " + NUM + r"% compounded annually\. What is its value after " + NUM + r" years\?"):
            principal, rate, years = m
            return "compound_interest", principal * (1 + rate / 100) ** years, f"{principal} * (1 + {rate}/100) ** {years}", "money"
        if m := match(r"A product line generates \$" + NUM + r" in revenue against \$" + NUM + r" in cost of goods sold\. What is the gross margin\?"):
            revenue, cost = m
            return "gross_margin", (revenue-cost)/revenue*100, f"({revenue} - {cost}) / {revenue} * 100", "percent"
        if m := match(r"An item listed at \$" + NUM + r" is discounted by " + NUM + r"%\. What is the sale price\?"):
            price, discount = m
            return "discount", price*(1-discount/100), f"{price} * (1 - {discount}/100)", "money"
        if m := match(r"Quarterly revenue moved from \$" + NUM + r" to \$" + NUM + r"\. What is the percentage change\?"):
            old, new = m
            return "percentage_change", (new-old)/old*100, f"({new} - {old}) / {old} * 100", "percent"
        if m := match(r"Fixed costs are \$" + NUM + r"\. Each unit sells for \$" + NUM + r" with a variable cost of \$" + NUM + r"\. How many units must be sold to break even, to the nearest unit\?"):
            fixed, price, variable = m
            if price <= variable:
                raise Unsolved("Nonpositive contribution margin")
            return "break_even", fixed/(price-variable), f"{fixed} / ({price} - {variable}); nearest unit as requested", "units"
        if m := match(r"A \$" + NUM + r" marketing budget is split between two channels in the ratio " + NUM + r":" + NUM + r"\. What does the first channel receive\?"):
            budget, first, second = m
            return "ratio_allocation", budget*first/(first+second), f"{budget} * {first} / ({first} + {second})", "money"
        if m := match(r"Region A had " + NUM + r" orders averaging \$" + NUM + r"; region B had " + NUM + r" orders averaging \$" + NUM + r"\. What is the overall average order value\?"):
            a, av, b, bv = m
            return "weighted_average", (a*av+b*bv)/(a+b), f"({a} * {av} + {b} * {bv}) / ({a} + {b})", "number"
        if m := match(r"An invoice for \$" + NUM + r" is converted to euros at " + NUM + r" euros per US dollar\. How many euros is that\?"):
            dollars, rate = m
            return "currency_conversion", dollars*rate, f"{dollars} * {rate}", "number"
        if m := match(r"A support queue holds " + NUM + r" billing tickets, " + NUM + r" technical tickets and " + NUM + r" account tickets\. If one is drawn at random, what is the probability it is a billing ticket\?"):
            billing, tech, account = m
            return "probability", billing/(billing+tech+account)*100, f"{billing} / ({billing} + {tech} + {account}) * 100", "percent"
        if m := match(r"A retailer sells " + NUM + r" units at \$" + NUM + r" each\. What is total revenue\?"):
            units, price = m
            return "revenue", units*price, f"{units} * {price}", "money"

    m = re.fullmatch(r"A monthly headcount series runs (\d+, \d+, \d+, \d+, \d+)\. If the pattern continues, what is the next value\?", text.strip())
    if m:
        values = [Decimal(x) for x in m[1].split(", ")]
        step = values[1]-values[0]
        if any(b-a != step for a, b in zip(values, values[1:])):
            raise Unsolved("Series is not arithmetic")
        return "arithmetic_series", values[-1]+step, f"{values[-1]} + {step}", "number"
    m = re.fullmatch(r"A contract signed on (\d{1,2}) ([A-Za-z]+) (\d{4}) has a (\d+)-day settlement period\. On what date does it settle\?", text.strip())
    if m and m[2] in MONTHS:
        start = datetime(int(m[3]), MONTHS.index(m[2])+1, int(m[1])).date()
        value = start + timedelta(days=int(m[4]))
        return "settlement_date", value, f"{start.isoformat()} + {m[4]} calendar days", "date"
    raise Unsolved(f"Unrecognized question: {text}")


def solve(row):
    kind, value, formula, unit = calculate(row["question_text"])
    matches = []
    for letter in "ABCD":
        option = row[f"option_{letter.lower()}"].strip()
        if unit == "date":
            m = re.fullmatch(r"(\d{1,2}) ([A-Za-z]+) (\d{4})", option)
            if m and m[2] in MONTHS:
                choice = datetime(int(m[3]), MONTHS.index(m[2])+1, int(m[1])).date()
                if choice == value:
                    matches.append(letter)
            continue
        pattern = {"money": r"\$(-?[\d,]+(?:\.\d+)?)", "percent": r"(-?[\d,]+(?:\.\d+)?)%", "units": r"(-?[\d,]+(?:\.\d+)?) units", "number": r"(-?[\d,]+(?:\.\d+)?)"}[unit]
        m = re.fullmatch(pattern, option)
        if not m:
            continue
        number = decimal(m[1])
        # Accept only the displayed rounding interval, never an arbitrary nearest choice.
        places = len(m[1].split(".")[1]) if "." in m[1] else 0
        tolerance = Decimal(10) ** -places / 2 + Decimal("1e-20")
        if abs(number-value) <= tolerance:
            matches.append(letter)
    if len(matches) != 1:
        raise Unsolved(f"Expected one matching option for {kind}, got {matches}; computed {value}")
    return Solution(matches[0], kind, str(value), formula)
