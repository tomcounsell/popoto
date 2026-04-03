"""Seed script to populate the database with sample data.

Generates:
- 100 restaurants across 10 cuisines
- 1,000 menu items (10 per restaurant)
- 500 customers
- 50 drivers with random locations
- 2,000 orders (mix of statuses)
- 100 review scores with confidence signals (v1.4.4)
"""

import random
from datetime import datetime, timedelta

from popoto import ConfidenceField

from .models import (
    CUISINES,
    Customer,
    Driver,
    MenuItem,
    Order,
    Restaurant,
    ReviewScore,
)

# Sample data for generating realistic names
FIRST_NAMES = [
    "Emma",
    "Liam",
    "Olivia",
    "Noah",
    "Ava",
    "Oliver",
    "Isabella",
    "Elijah",
    "Sophia",
    "Lucas",
    "Mia",
    "Mason",
    "Charlotte",
    "Logan",
    "Amelia",
    "James",
    "Harper",
    "Aiden",
    "Evelyn",
    "Ethan",
    "Aria",
    "Alexander",
    "Luna",
    "Henry",
    "Chloe",
    "Sebastian",
    "Penelope",
    "Jack",
    "Layla",
    "Daniel",
    "Riley",
]

LAST_NAMES = [
    "Smith",
    "Johnson",
    "Williams",
    "Brown",
    "Jones",
    "Garcia",
    "Miller",
    "Davis",
    "Rodriguez",
    "Martinez",
    "Anderson",
    "Taylor",
    "Thomas",
    "Moore",
    "Jackson",
    "Martin",
    "Lee",
    "Thompson",
    "White",
    "Harris",
    "Clark",
]

RESTAURANT_ADJECTIVES = [
    "Golden",
    "Royal",
    "Happy",
    "Lucky",
    "Jade",
    "Dragon",
    "Phoenix",
    "Blue",
    "Red",
    "Green",
    "Silver",
    "Sunset",
    "Morning",
    "Garden",
    "Ocean",
    "Mountain",
]

RESTAURANT_NOUNS = [
    "Palace",
    "Kitchen",
    "Garden",
    "House",
    "Express",
    "Grill",
    "Cafe",
    "Bistro",
    "Diner",
    "Eatery",
    "Corner",
    "Place",
    "Spot",
    "Table",
    "Bowl",
    "Wok",
]

# Menu items by cuisine
MENU_ITEMS = {
    "Italian": [
        ("Margherita Pizza", 14.99, "Main"),
        ("Spaghetti Carbonara", 16.99, "Main"),
        ("Bruschetta", 8.99, "Appetizer"),
        ("Tiramisu", 7.99, "Dessert"),
        ("Caesar Salad", 10.99, "Appetizer"),
        ("Lasagna", 15.99, "Main"),
        ("Garlic Bread", 5.99, "Side"),
        ("Gelato", 6.99, "Dessert"),
        ("Minestrone Soup", 7.99, "Appetizer"),
        ("Penne Arrabbiata", 13.99, "Main"),
    ],
    "Chinese": [
        ("Kung Pao Chicken", 13.99, "Main"),
        ("Sweet and Sour Pork", 14.99, "Main"),
        ("Spring Rolls", 6.99, "Appetizer"),
        ("Fried Rice", 10.99, "Main"),
        ("Dim Sum Platter", 12.99, "Appetizer"),
        ("Mapo Tofu", 11.99, "Main"),
        ("Hot and Sour Soup", 5.99, "Appetizer"),
        ("Fortune Cookies", 2.99, "Dessert"),
        ("Beef Chow Mein", 13.99, "Main"),
        ("General Tso's Chicken", 14.99, "Main"),
    ],
    "Japanese": [
        ("Salmon Sashimi", 18.99, "Main"),
        ("Chicken Teriyaki", 14.99, "Main"),
        ("Miso Soup", 4.99, "Appetizer"),
        ("Edamame", 5.99, "Appetizer"),
        ("Ramen", 13.99, "Main"),
        ("California Roll", 12.99, "Main"),
        ("Gyoza", 7.99, "Appetizer"),
        ("Mochi Ice Cream", 6.99, "Dessert"),
        ("Udon", 12.99, "Main"),
        ("Tempura", 11.99, "Appetizer"),
    ],
    "Mexican": [
        ("Tacos al Pastor", 12.99, "Main"),
        ("Burrito Bowl", 13.99, "Main"),
        ("Guacamole & Chips", 8.99, "Appetizer"),
        ("Quesadilla", 10.99, "Main"),
        ("Churros", 6.99, "Dessert"),
        ("Nachos Supreme", 11.99, "Appetizer"),
        ("Enchiladas", 14.99, "Main"),
        ("Mexican Rice", 4.99, "Side"),
        ("Fajitas", 16.99, "Main"),
        ("Tres Leches Cake", 7.99, "Dessert"),
    ],
    "Indian": [
        ("Butter Chicken", 15.99, "Main"),
        ("Chicken Tikka Masala", 16.99, "Main"),
        ("Samosas", 6.99, "Appetizer"),
        ("Naan Bread", 3.99, "Side"),
        ("Biryani", 14.99, "Main"),
        ("Palak Paneer", 13.99, "Main"),
        ("Mango Lassi", 4.99, "Drink"),
        ("Gulab Jamun", 5.99, "Dessert"),
        ("Dal Tadka", 10.99, "Main"),
        ("Tandoori Chicken", 15.99, "Main"),
    ],
    "Thai": [
        ("Pad Thai", 13.99, "Main"),
        ("Green Curry", 14.99, "Main"),
        ("Tom Yum Soup", 7.99, "Appetizer"),
        ("Spring Rolls", 6.99, "Appetizer"),
        ("Massaman Curry", 15.99, "Main"),
        ("Thai Fried Rice", 12.99, "Main"),
        ("Mango Sticky Rice", 7.99, "Dessert"),
        ("Papaya Salad", 9.99, "Appetizer"),
        ("Pad See Ew", 13.99, "Main"),
        ("Thai Iced Tea", 4.99, "Drink"),
    ],
    "American": [
        ("Classic Burger", 12.99, "Main"),
        ("BBQ Ribs", 18.99, "Main"),
        ("Buffalo Wings", 10.99, "Appetizer"),
        ("Mac and Cheese", 9.99, "Side"),
        ("Apple Pie", 6.99, "Dessert"),
        ("Caesar Salad", 8.99, "Appetizer"),
        ("Philly Cheesesteak", 14.99, "Main"),
        ("Onion Rings", 6.99, "Side"),
        ("Milkshake", 5.99, "Drink"),
        ("Hot Dog", 7.99, "Main"),
    ],
    "Mediterranean": [
        ("Falafel Wrap", 11.99, "Main"),
        ("Hummus Platter", 8.99, "Appetizer"),
        ("Greek Salad", 10.99, "Appetizer"),
        ("Shawarma", 13.99, "Main"),
        ("Baklava", 5.99, "Dessert"),
        ("Lamb Kebab", 16.99, "Main"),
        ("Tabbouleh", 7.99, "Side"),
        ("Pita Bread", 3.99, "Side"),
        ("Moussaka", 14.99, "Main"),
        ("Stuffed Grape Leaves", 8.99, "Appetizer"),
    ],
    "Korean": [
        ("Bibimbap", 14.99, "Main"),
        ("Korean BBQ", 19.99, "Main"),
        ("Kimchi Fried Rice", 12.99, "Main"),
        ("Japchae", 13.99, "Main"),
        ("Kimchi", 4.99, "Side"),
        ("Korean Fried Chicken", 15.99, "Main"),
        ("Tteokbokki", 10.99, "Appetizer"),
        ("Bulgogi", 16.99, "Main"),
        ("Pajeon", 9.99, "Appetizer"),
        ("Bingsu", 8.99, "Dessert"),
    ],
    "Vietnamese": [
        ("Pho", 12.99, "Main"),
        ("Banh Mi", 9.99, "Main"),
        ("Spring Rolls", 7.99, "Appetizer"),
        ("Bun Bo Hue", 13.99, "Main"),
        ("Vietnamese Coffee", 4.99, "Drink"),
        ("Vermicelli Bowl", 12.99, "Main"),
        ("Lemongrass Chicken", 13.99, "Main"),
        ("Com Tam", 11.99, "Main"),
        ("Che", 5.99, "Dessert"),
        ("Goi Cuon", 8.99, "Appetizer"),
    ],
}

# NYC area coordinates for realistic geo data
NYC_CENTER = (40.7128, -74.0060)
NYC_RADIUS = 0.15  # ~10 miles in lat/long degrees


def random_location() -> tuple:
    """Generate random location around NYC."""
    lat = NYC_CENTER[0] + random.uniform(-NYC_RADIUS, NYC_RADIUS)
    lng = NYC_CENTER[1] + random.uniform(-NYC_RADIUS, NYC_RADIUS)
    return (round(lat, 6), round(lng, 6))


def random_name() -> str:
    """Generate random person name."""
    return f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"


def random_username(name: str) -> str:
    """Generate username from name."""
    parts = name.lower().split()
    suffix = random.randint(1, 999)
    return f"{parts[0]}{parts[1][0]}{suffix}"


def random_phone() -> str:
    """Generate random phone number."""
    return f"+1{random.randint(200, 999)}{random.randint(100, 999)}{random.randint(1000, 9999)}"


def random_email(name: str) -> str:
    """Generate email from name."""
    domains = ["gmail.com", "yahoo.com", "outlook.com", "email.com"]
    parts = name.lower().split()
    return f"{parts[0]}.{parts[1]}{random.randint(1, 99)}@{random.choice(domains)}"


def restaurant_name(cuisine: str) -> str:
    """Generate restaurant name."""
    adj = random.choice(RESTAURANT_ADJECTIVES)
    noun = random.choice(RESTAURANT_NOUNS)
    return f"{adj} {cuisine} {noun}"


def clear_database():
    """Clear all existing data including secondary indexes."""
    # Delete models with relationships first, then referenced models
    for model in [Order, MenuItem, Customer, Driver, ReviewScore, Restaurant]:
        model.delete_all()
    print("Cleared all Popoto Kitchen data")


def seed_database(
    num_restaurants: int = 100,
    num_customers: int = 500,
    num_drivers: int = 50,
    num_orders: int = 2000,
):
    """Seed database with sample data."""
    print(f"Seeding {num_restaurants} restaurants...")
    restaurants = []
    used_names = set()

    for i in range(num_restaurants):
        cuisine = CUISINES[i % len(CUISINES)]
        name = restaurant_name(cuisine)
        # Ensure unique names
        while name in used_names:
            name = restaurant_name(cuisine)
        used_names.add(name)

        restaurant = Restaurant(
            name=name,
            cuisine=cuisine,
            rating=round(random.uniform(3.0, 5.0), 1),
            location=random_location(),
            active=random.random() > 0.1,  # 90% active
        )
        restaurant.save()
        restaurants.append(restaurant)

    print("Seeding menu items...")
    menu_items = []
    for restaurant in restaurants:
        cuisine_items = MENU_ITEMS.get(restaurant.cuisine, MENU_ITEMS["American"])
        for name, base_price, category in cuisine_items:
            # Vary prices slightly
            price = round(base_price * random.uniform(0.9, 1.1), 2)
            item = MenuItem(
                name=name,
                description=f"Delicious {name.lower()} from {restaurant.name}",
                price=price,
                restaurant=restaurant,
                available=random.random() > 0.05,  # 95% available
                category=category,
            )
            item.save()
            menu_items.append(item)

    print(f"Seeding {num_customers} customers...")
    customers = []
    used_usernames = set()
    used_emails = set()

    for _ in range(num_customers):
        name = random_name()
        username = random_username(name)
        email = random_email(name)

        # Ensure unique
        while username in used_usernames:
            username = f"{username}{random.randint(1, 99)}"
        while email in used_emails:
            email = f"{random.randint(1, 999)}{email}"

        used_usernames.add(username)
        used_emails.add(email)

        customer = Customer(
            username=username,
            email=email,
            name=name,
            phone=random_phone(),
            address=random_location(),
        )
        customer.save()
        customers.append(customer)

    print(f"Seeding {num_drivers} drivers...")
    drivers = []
    used_phones = set()

    for _ in range(num_drivers):
        name = random_name()
        phone = random_phone()
        while phone in used_phones:
            phone = random_phone()
        used_phones.add(phone)

        driver = Driver(
            name=name,
            phone=phone,
            rating=round(random.uniform(3.5, 5.0), 1),
            location=random_location(),
            active=random.random() > 0.3,  # 70% active
            vehicle=random.choice(["car", "bike", "scooter"]),
        )
        driver.save()
        drivers.append(driver)

    print(f"Seeding {num_orders} orders...")
    status_weights = {
        "pending": 0.1,
        "confirmed": 0.1,
        "preparing": 0.15,
        "ready": 0.1,
        "delivering": 0.15,
        "delivered": 0.35,
        "cancelled": 0.05,
    }
    statuses = list(status_weights.keys())
    weights = list(status_weights.values())

    for i in range(num_orders):
        customer = random.choice(customers)
        restaurant = random.choice(restaurants)
        status = random.choices(statuses, weights=weights)[0]

        # Pick 1-5 random items from the restaurant's menu
        restaurant_items = [
            m
            for m in menu_items
            if m.restaurant.db_key.redis_key == restaurant.db_key.redis_key
        ]
        if restaurant_items:
            order_items = random.sample(
                restaurant_items, min(random.randint(1, 5), len(restaurant_items))
            )
            item_names = [item.name for item in order_items]
            total = sum(item.price for item in order_items)
        else:
            item_names = ["Special of the Day"]
            total = round(random.uniform(15.0, 50.0), 2)

        # Assign driver if order is in delivery phase
        driver = None
        if status in ["ready", "delivering", "delivered"]:
            active_drivers = [d for d in drivers if d.active]
            if active_drivers:
                driver = random.choice(active_drivers)

        # Random timestamp in last 30 days
        days_ago = random.randint(0, 30)
        hours_ago = random.randint(0, 23)
        created = datetime.utcnow() - timedelta(days=days_ago, hours=hours_ago)

        order = Order(
            customer=customer,
            restaurant=restaurant,
            driver=driver,
            items=item_names,
            total=round(total, 2),
            status=status,
            notes=(
                ""
                if random.random() > 0.2
                else random.choice(
                    [
                        "Extra napkins please",
                        "Ring doorbell",
                        "Leave at door",
                        "No onions",
                        "Extra sauce",
                        "Call on arrival",
                    ]
                )
            ),
            created_at=created.isoformat(),
            updated_at=created.isoformat(),
        )
        order.save()

        if (i + 1) % 500 == 0:
            print(f"  Created {i + 1} orders...")

    # Seed review scores using ConfidenceField
    review_count = seed_review_scores(restaurants, customers)

    print("\nSeeding complete!")
    print(f"  - {len(restaurants)} restaurants")
    print(f"  - {len(menu_items)} menu items")
    print(f"  - {len(customers)} customers")
    print(f"  - {len(drivers)} drivers")
    print(f"  - {num_orders} orders")
    print(f"  - {review_count} review scores")



def seed_review_scores(restaurants, customers, reviews_per_restaurant=5):
    """Seed ReviewScore instances with Bayesian confidence signals.

    Creates review scores for a subset of customer/restaurant pairs and
    applies varied confidence signals to build evidence history. This
    demonstrates ConfidenceField's Bayesian update mechanism where each
    signal nudges the confidence toward 0 or 1 with diminishing effect.

    Args:
        restaurants: List of Restaurant instances to review.
        customers: List of Customer instances who leave reviews.
        reviews_per_restaurant: Number of reviews per restaurant (default 5).

    Returns:
        int: Total number of ReviewScore instances created.
    """
    print("Seeding review scores...")
    count = 0

    # Use a subset of restaurants (first 20) and random customers
    sample_restaurants = restaurants[:20]

    for restaurant in sample_restaurants:
        # Pick random customers to be reviewers for this restaurant
        reviewers = random.sample(
            customers, min(reviews_per_restaurant, len(customers))
        )

        for customer in reviewers:
            review = ReviewScore(
                restaurant=restaurant.name,
                reviewer=customer.username,
            )
            review.save()

            # Apply 2-6 confidence signals to build evidence history.
            # Signals near 1.0 = positive review, near 0.0 = negative.
            # Mix of signals creates realistic confidence distributions.
            num_signals = random.randint(2, 6)
            for _ in range(num_signals):
                # Most reviews are positive (0.6-0.95), some negative (0.1-0.4)
                if random.random() > 0.25:
                    signal = random.uniform(0.6, 0.95)
                else:
                    signal = random.uniform(0.1, 0.4)
                ConfidenceField.update_confidence(review, "score", signal=signal)

            count += 1

    print(f"  Created {count} review scores with confidence signals")
    return count


if __name__ == "__main__":
    seed_database()
